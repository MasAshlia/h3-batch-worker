"""Local, dependency-free browser UI for guarded H3 episode generation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import threading
from urllib.parse import unquote, urlparse
from uuid import uuid4
import webbrowser

from .batch import compile_batch, compile_episode_folder
from .comfy_client import RealComfyClient
from .config import Config, load_config
from .errors import ConfigurationError, WorkerError
from .models import BatchReport, TaskStatus
from .parser import parse_shot_identity
from .queue_manager import QueueManager


_SHOT_HEADER = re.compile(
    r"^\s*(?:#+\s*)?SHOT\s+(?:[0-9]+-[0-9]+|EP[0-9]+-S[0-9]+[A-Z]?|E[0-9]+-[0-9]+[A-Z]?)\b",
    re.IGNORECASE,
)
_SHOT_START = re.compile(
    r"^\s*(?:#+\s*)?SHOT\s+(?:[0-9]+-[0-9]+|EP[0-9]+-S[0-9]+[A-Z]?|E[0-9]+-[0-9]+[A-Z]?)\b[^\r\n]*\bSTART\b",
    re.IGNORECASE,
)
_EPISODE = re.compile(r"^SHOT\s+([0-9]+)-[0-9]+\b", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def split_episode_text(text: str) -> list[str]:
    """Split shot-first and duration-first production master TXT files."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    explicit_starts = [index for index, line in enumerate(lines) if _SHOT_START.match(line.strip())]
    if explicit_starts:
        starts = explicit_starts
    else:
        starts = []
    if not starts:
        for index, line in enumerate(lines):
            current = line.strip()
            next_nonempty = next(
                (candidate.strip() for candidate in lines[index + 1:] if candidate.strip()), "",
            )
            if (_SHOT_HEADER.match(current) and next_nonempty.upper().startswith("SHOT DURATION:")):
                starts.append(index)
            elif (current.upper().startswith("SHOT DURATION:")
                  and _SHOT_HEADER.match(next_nonempty)):
                starts.append(index)
    if not starts:
        raise ConfigurationError("TXT contains no recognizable SHOT / SHOT DURATION pair")
    starts = sorted(set(starts))
    starts.append(len(lines))
    return ["".join(lines[left:right]).strip() + "\n" for left, right in zip(starts, starts[1:])]


def _report_json(report: BatchReport, asset_tokens: dict[str, Path]) -> dict:
    shots = []
    for item in report.items:
        if item.errors:
            shots.append({"shot_id": item.shot_id, "ready": False, "errors": item.errors})
            continue
        compiled = item.compiled
        mappings = []
        for slot, asset_id in compiled.task.reference_slots.items():
            audit = compiled.asset_audit[slot]
            path = compiled.assets[slot]
            token = uuid4().hex
            asset_tokens[token] = path
            workflow = compiled.workflow_mapping[slot]
            mappings.append({
                "slot": slot,
                "picture_number": workflow.get("picture_number"),
                "asset_id": asset_id,
                "filename": path.name,
                "path": str(path),
                "source": audit.get("source"),
                "sha256": audit.get("sha256"),
                "candidates": audit.get("candidates", []),
                "h3_input": workflow.get("h3_input"),
                "load_node": workflow.get("load_node"),
                "thumbnail": f"/api/assets/{token}",
            })
        task = compiled.task
        shots.append({
            "shot_id": task.shot_id,
            "ready": True,
            "raw_duration": task.raw_duration_seconds,
            "duration": task.duration_seconds,
            "steps": task.steps,
            "expected_frames": task.expected_frame_count,
            "expected_output_seconds": task.expected_output_seconds,
            "transformations": task.transformations,
            "mappings": mappings,
        })
    return {"episode": report.episode, "ready": report.ready, "shots": shots}


@dataclass
class _Preflight:
    sources: tuple[Path, ...]
    response: dict
    job_root: Path
    input_files: tuple[dict, ...]
    config: Config


class UiController:
    def __init__(self, config: Config, *, workspace: Path | None = None):
        self.config = config
        self.workspace = (workspace or config.output_root.parent / "ui_jobs").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.upload_root = (self.workspace.parent / "codex_uploads").resolve()
        self._lock = threading.Lock()
        self._preflights: dict[str, _Preflight] = {}
        self._asset_tokens: dict[str, Path] = {}
        self._active_thread: threading.Thread | None = None
        self._active_run: Path | None = None
        self._active_session: Path | None = None
        self._service_error: dict | None = None
        from .review import ReviewService
        self.reviews = ReviewService(self)

    def _bundle_config(self, asset_bundle_id: str | None) -> Config:
        if asset_bundle_id is None:
            return self.config
        if not isinstance(asset_bundle_id, str) or not re.fullmatch(r"[0-9a-f]{32}", asset_bundle_id):
            raise ConfigurationError("Invalid asset bundle ID")
        bundle = (self.upload_root / asset_bundle_id).resolve()
        if not bundle.is_relative_to(self.upload_root):
            raise ConfigurationError("Invalid asset bundle path")
        assets = bundle / "assets"
        manifest = bundle / "asset_manifest.json"
        if not assets.is_dir() or not manifest.is_file():
            raise ConfigurationError("Asset bundle is incomplete")
        return replace(
            self.config,
            asset_manifest=manifest,
            asset_roots=(assets,),
            asset_index=bundle / "asset_index.generated.json",
        )

    def preflight(self, files: list[dict], asset_bundle_id: str | None = None) -> dict:
        if not isinstance(files, list) or not files:
            raise ConfigurationError("Choose at least one Prompt file")
        job_config = self._bundle_config(asset_bundle_id)
        job_id = uuid4().hex
        job_root = self.workspace / job_id
        job_root.mkdir(parents=True)
        reference_limit = RealComfyClient(self.config.base_url).get_h3_reference_limit()
        input_files = []
        for uploaded in files:
            name, content = uploaded.get("name"), uploaded.get("text")
            if not isinstance(name, str) or not isinstance(content, str):
                raise ConfigurationError("Uploaded file must contain text")
            input_files.append({
                "name": Path(name).name,
                "sha256": sha256(content.encode("utf-8")).hexdigest(),
            })
        reports = []
        sources = []
        if len(files) == 1 and Path(str(files[0].get("name", ""))).suffix.lower() in {".json", ".yaml", ".yml"}:
            name, content = files[0].get("name"), files[0].get("text")
            source = job_root / Path(name).name
            source.write_text(content, encoding="utf-8")
            report, _ = compile_batch(source, job_config, max_reference_images=reference_limit)
            reports.append(report)
            sources.append(source)
        else:
            prompts = []
            for uploaded in files:
                name, content = uploaded.get("name"), uploaded.get("text")
                if (not isinstance(name, str) or Path(name).suffix.lower() not in {".txt", ".md"}
                        or not isinstance(content, str) or len(content.encode("utf-8")) > 5_000_000):
                    raise ConfigurationError("Prompt uploads must be UTF-8 TXT files under 5 MB")
                prompts.extend(split_episode_text(content))
            grouped: dict[str, list[tuple[int, str, str]]] = {}
            for prompt in prompts:
                episode, shot_id, order = parse_shot_identity(prompt)
                grouped.setdefault(episode, []).append((order, shot_id, prompt))
            for episode in sorted(grouped, key=lambda value: int(value.removeprefix("EP"))):
                source = job_root / f"{episode}.ready"
                source.mkdir()
                seen = set()
                for order, shot_id, prompt in sorted(grouped[episode]):
                    if shot_id in seen:
                        raise ConfigurationError(f"Duplicate shot in upload: {shot_id}")
                    seen.add(shot_id)
                    (source / f"SHOT_{order:04d}_{shot_id}.txt").write_text(prompt, encoding="utf-8")
                report, _ = compile_episode_folder(
                    source, job_config, max_reference_images=reference_limit,
                )
                reports.append(report)
                sources.append(source)
        combined = BatchReport(
            reports[0].episode if len(reports) == 1 else f"{reports[0].episode}–{reports[-1].episode}",
            [item for report in reports for item in report.items],
        )
        with self._lock:
            response = {
                "preflight_id": job_id,
                "episodes": [report.episode for report in reports],
                "input_files": input_files,
                **_report_json(combined, self._asset_tokens),
            }
            self._preflights[job_id] = _Preflight(
                tuple(sources), response, job_root, tuple(input_files), job_config,
            )
        return response

    def start(self, preflight_id: str, generation_counts: dict, run_name: str | None = None, *, _from_queue=False) -> dict:
        with self._lock:
            if not _from_queue and self.reviews.pending():
                raise ConfigurationError("Confirmed video revisions are queued; wait for the queue to finish")
            preflight = self._preflights.get(preflight_id)
            if preflight is None:
                raise ConfigurationError("Preflight has expired; upload the Prompt again")
            if self._active_thread is not None and self._active_thread.is_alive():
                raise ConfigurationError("Another episode is already running")
            expected = {
                shot["shot_id"] for shot in preflight.response["shots"] if shot.get("ready")
            }
            if (not preflight.response["ready"] or not isinstance(generation_counts, dict)
                    or set(generation_counts) != expected
                    or any(type(value) is not int or not 1 <= value <= 4
                           for value in generation_counts.values())):
                raise ConfigurationError("Every ready shot needs a generation count from one to four")
            self._active_run = None
            session_path = preflight.job_root / "session.json"
            session = {
                "session_id": preflight_id,
                "episode": preflight.response["episode"],
                "episodes": preflight.response.get("episodes", []),
                "input_files": list(preflight.input_files),
                "planned_generations": sum(generation_counts.values()),
                "child_runs": [],
                "status": "RUNNING",
                "started_at": _now(),
                "finished_at": None,
                "error": None,
            }
            _write_json(session_path, session)
            self._active_session = session_path
            self._service_error = None
            thread = threading.Thread(
                target=self._run, args=(preflight, dict(generation_counts), run_name), daemon=True,
            )
            self._active_thread = thread
            thread.start()
        return {
            "started": True,
            "session_id": preflight_id,
            "planned_generations": sum(generation_counts.values()),
        }

    def _update_session(self, **updates) -> None:
        with self._lock:
            path = self._active_session
            if path is None:
                return
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            session.update(updates)
            _write_json(path, session)

    def _run(self, preflight: _Preflight, generation_counts: dict, run_name: str | None = None) -> None:
        try:
            client = RealComfyClient(self.config.base_url)
            reference_limit = client.get_h3_reference_limit()
            stats = client.get_system_stats()
            if not isinstance(stats.get("system"), dict) or not isinstance(stats.get("devices"), list):
                raise ConfigurationError("ComfyUI system preflight failed")
            for source in preflight.sources:
                if source.is_dir():
                    report, patcher = compile_episode_folder(
                        source, preflight.config, max_reference_images=reference_limit,
                    )
                else:
                    report, patcher = compile_batch(
                        source, preflight.config, max_reference_images=reference_limit,
                    )
                if not report.ready or patcher.is_test_fixture:
                    raise ConfigurationError("Production preflight no longer passes")

                def created(path: Path) -> None:
                    with self._lock:
                        self._active_run = path
                        session_path = self._active_session
                        if session_path is None:
                            return
                        session = json.loads(session_path.read_text(encoding="utf-8"))
                        session["child_runs"].append({
                            "episode": report.episode, "batch_id": path.parent.name,
                        })
                        _write_json(session_path, session)

                episode_counts = {
                    task.task.shot_id: generation_counts[task.task.shot_id]
                    for task in report.tasks
                }
                _, record = QueueManager(
                    client, patcher, preflight.config.output_root,
                    max_attempts=1,
                    require_queue_confirmation=True,
                    require_video_output=True,
                    require_idle_before_each=True,
                    stop_on_failure=True,
                    debug_root=preflight.config.debug_root,
                    comfy_output=preflight.config.comfy_output,
                    on_run_created=created,
                ).run(report, generation_counts=episode_counts, run_name=run_name)
                if record["summary"]["failed"]:
                    raise ConfigurationError(
                        "Production stopped after an episode generation failed",
                        details={"episode": report.episode, "batch_id": record["batch_id"]},
                    )
            self._update_session(status="SUCCESS", finished_at=_now())
        except WorkerError as exc:
            with self._lock:
                self._service_error = exc.to_dict()
            self._update_session(status="FAILED", finished_at=_now(), error=exc.to_dict())
        except Exception:
            error = {
                "type": "UnexpectedUiWorkerError",
                "message": "The local generation worker stopped unexpectedly",
            }
            with self._lock:
                self._service_error = error
            self._update_session(status="FAILED", finished_at=_now(), error=error)

    def state(self) -> dict:
        with self._lock:
            thread = self._active_thread
            run_path = self._active_run
            session_path = self._active_session
            service_error = self._service_error
        record = None
        if run_path is not None and run_path.is_file():
            try:
                record = json.loads(run_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        running = bool(thread and thread.is_alive())
        session = None
        if session_path is not None and session_path.is_file():
            try:
                session = json.loads(session_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        result = {
            "running": running, "service_error": service_error,
            "run": record, "session": session,
            "capabilities": {"codex_asset_bundles": True},
        }
        if session is not None:
            completed = 0
            for child in session.get("child_runs", []):
                child_path = self.config.output_root / child.get("batch_id", "") / "run.json"
                try:
                    child_record = json.loads(child_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                completed += sum(
                    shot.get("status") in {
                        TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.SKIPPED,
                        TaskStatus.UNKNOWN_SUBMISSION,
                    }
                    for shot in child_record.get("shots", [])
                )
            result["progress"] = {
                "completed": completed,
                "total": session.get("planned_generations", 0),
            }
        elif record is not None:
            completed = sum(
                shot.get("status") in {
                    TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.SKIPPED,
                    TaskStatus.UNKNOWN_SUBMISSION,
                }
                for shot in record.get("shots", [])
            )
            result["progress"] = {
                "completed": completed,
                "total": record.get("planned_generations", len(record.get("shots", []))),
            }
        return result

    def history(self) -> list[dict]:
        rows = []
        if not self.config.output_root.is_dir():
            return rows
        for path in sorted(self.config.output_root.glob("*/run.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({
                "batch_id": record.get("batch_id"),
                "episode": record.get("episode"),
                "started_at": record.get("started_at"),
                "finished_at": record.get("finished_at"),
                "summary": record.get("summary"),
                "video_count": sum(bool(shot.get("output")) for shot in record.get("shots", [])),
            })
        return rows

    def run_record(self, batch_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", batch_id):
            raise ConfigurationError("Invalid run id")
        path = self.config.output_root / batch_id / "run.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Run record is unavailable") from exc

    def asset_path(self, token: str) -> Path:
        with self._lock:
            path = self._asset_tokens.get(token)
        if path is None or not path.is_file():
            raise ConfigurationError("Asset preview is unavailable")
        return path

    def media_path(self, batch_id: str, shot_index: int) -> Path:
        record = self.run_record(batch_id)
        shots = record.get("shots", [])
        if not 0 <= shot_index < len(shots):
            raise ConfigurationError("Video index is unavailable")
        output = shots[shot_index].get("output")
        if not isinstance(output, dict) or self.config.comfy_output is None:
            raise ConfigurationError("Video output is unavailable")
        root = self.config.comfy_output.resolve()
        path = (root / output.get("subfolder", "") / output.get("filename", "")).resolve()
        if root != path and root not in path.parents:
            raise ConfigurationError("Video path is outside the configured output folder")
        if not path.is_file():
            raise ConfigurationError("Video file is unavailable")
        return path


class UiHandler(BaseHTTPRequestHandler):
    controller: UiController
    html_path: Path

    def log_message(self, format: str, *args) -> None:
        pass

    def _json(self, value, status=HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, exc: Exception, status=HTTPStatus.BAD_REQUEST) -> None:
        value = exc.to_dict() if isinstance(exc, WorkerError) else {
            "type": type(exc).__name__, "message": "Local UI request failed",
        }
        self._json({"error": value}, status)

    def do_POST(self) -> None:
        try:
            if self.headers.get_content_type() != "application/json":
                raise ConfigurationError("JSON requests are required")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise ConfigurationError("Cross-origin production requests are not allowed")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65_000_000:
                raise ConfigurationError("Request body is empty or too large")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ConfigurationError("Request must contain an object")
            if self.path == "/api/ai/settings":
                self._json(self.controller.reviews.settings(body))
            elif self.path == "/api/ai/test":
                self.controller.reviews.call_ai([{"role": "user", "content": "Reply OK."}])
                self._json({"ok": True})
            elif self.path == "/api/reviews":
                self._json(self.controller.reviews.create(body.get("batch_id"), body.get("shot_index")))
            elif self.path.startswith("/api/reviews/"):
                parts = self.path.split("/")
                if len(parts) != 5:
                    raise ConfigurationError("Invalid review action")
                review_id, action = parts[3:]
                service = self.controller.reviews
                if action == "save":
                    result = service.update(review_id, body)
                elif action == "rewrite":
                    result = service.rewrite(review_id)
                elif action == "preflight":
                    result = service.preflight(review_id)
                elif action == "enqueue":
                    result = service.enqueue(review_id)
                else:
                    raise ConfigurationError("Unknown review action")
                self._json(result)
            elif self.path == "/api/preflight":
                self._json(self.controller.preflight(
                    body.get("files"), body.get("asset_bundle_id"),
                ))
            elif self.path == "/api/runs":
                self._json(self.controller.start(
                    body.get("preflight_id"), body.get("generation_counts"), body.get("run_name"),
                ), HTTPStatus.ACCEPTED)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (WorkerError, ValueError, TypeError, OSError, KeyError) as exc:
            self._error(exc)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/":
                payload = self.html_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/review.js":
                self._send_file(self.html_path.with_name("review.js"))
            elif path == "/api/state":
                self._json(self.controller.state())
            elif path == "/api/ai/settings":
                self._json(self.controller.reviews.settings())
            elif path == "/api/reviews":
                self._json({"reviews": [{k: v for k, v in item.items() if k in {
                    "id", "source_batch", "source_index", "shot_id", "status", "summary", "session_id", "child_runs", "error", "created_at",
                }} for item in self.controller.reviews.items()]})
            elif path.startswith("/api/reviews/"):
                self._json(self.controller.reviews.get(path.removeprefix("/api/reviews/")))
            elif path == "/api/runs":
                self._json({"runs": self.controller.history()})
            elif path.startswith("/api/runs/"):
                self._json(self.controller.run_record(path.removeprefix("/api/runs/")))
            elif path.startswith("/api/assets/"):
                self._send_file(self.controller.asset_path(path.removeprefix("/api/assets/")))
            elif path.startswith("/media/"):
                parts = path.split("/")
                if len(parts) != 4 or not parts[3].isdigit():
                    raise ConfigurationError("Invalid media URL")
                self._send_file(self.controller.media_path(parts[2], int(parts[3])), ranges=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except WorkerError as exc:
            self._error(exc, HTTPStatus.NOT_FOUND)

    def _send_file(self, path: Path, *, ranges: bool = False) -> None:
        size = path.stat().st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        requested = self.headers.get("Range") if ranges else None
        if requested:
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", requested)
            if match is None:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            if match[1]:
                start = int(match[1])
            if match[2]:
                end = int(match[2])
            if not 0 <= start <= end < size:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def serve(config_path: Path, *, port: int = 8765, open_browser: bool = True) -> None:
    if not 1 <= port <= 65535:
        raise ConfigurationError("UI port must be between 1 and 65535")
    controller = UiController(load_config(config_path.resolve()))
    html_path = Path(__file__).with_name("ui.html")
    handler = type("BoundUiHandler", (UiHandler,), {
        "controller": controller,
        "html_path": html_path,
    })
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    controller.reviews.start_dispatcher()
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.3, partial(webbrowser.open, url)).start()
    print(f"H3 visual console: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.reviews.stop.set()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H3 local visual console")
    parser.add_argument("--config", type=Path, default=Path("private/config.windows.yaml"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    try:
        serve(args.config, port=args.port, open_browser=not args.no_open)
        return 0
    except WorkerError as exc:
        print(f"ERROR {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
