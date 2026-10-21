#!/usr/bin/env python3
"""Small stdlib bridge from Codex attachments to the local H3 UI API."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import zipfile


REPOSITORY = "https://github.com/MasAshlia/h3-batch-worker.git"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PROMPT_SUFFIXES = {".txt", ".md", ".json", ".yaml", ".yml"}
MAX_FILES = 500
MAX_UNCOMPRESSED_BYTES = 2_000_000_000


class BridgeError(RuntimeError):
    pass


def _json_dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _is_project(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "setup-production.cmd").is_file()


def find_project(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    marker = Path(__file__).resolve().parents[1] / "project-root.txt"
    if marker.is_file():
        candidates.append(Path(marker.read_text(encoding="utf-8-sig").strip()))
    if os.environ.get("H3_BATCH_WORKER_HOME"):
        candidates.append(Path(os.environ["H3_BATCH_WORKER_HOME"]))
    current = Path.cwd().resolve()
    candidates.extend([current, *current.parents])
    user = Path.home()
    candidates.extend([
        user / "Documents" / "h3-batch-worker",
        user / "Downloads" / "h3-batch-worker-production",
    ])
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if _is_project(resolved):
            return resolved
    raise BridgeError("h3-batch-worker is not installed; run bootstrap first")


def bootstrap(project: str | None) -> dict:
    try:
        root = find_project(project)
    except BridgeError:
        root = Path(project).expanduser().resolve() if project else Path.home() / "Documents" / "h3-batch-worker"
        if root.exists():
            raise BridgeError(f"installation target already exists but is not h3-batch-worker: {root}")
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--branch", "main", "--single-branch", REPOSITORY, str(root)], check=True)
    subprocess.run(["cmd", "/c", str(root / "setup-production.cmd"), "--no-pause"], cwd=root, check=True)
    return {"installed": True, "project": str(root)}


def _copy_unique(source, destination: Path, seen: set[str]) -> None:
    key = str(destination).casefold()
    if key in seen or destination.exists():
        raise BridgeError(f"duplicate asset package path: {destination.name}")
    seen.add(key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(source, "read"):
        with destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    else:
        shutil.copy2(source, destination)


def _safe_zip_member(info: zipfile.ZipInfo) -> PurePosixPath:
    member = PurePosixPath(info.filename.replace("\\", "/"))
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise BridgeError(f"unsafe ZIP path: {info.filename}")
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        raise BridgeError(f"ZIP symbolic links are not allowed: {info.filename}")
    return member


def _collect_assets(inputs: list[Path], assets_root: Path) -> Path | None:
    seen: set[str] = set()
    manifests: list[Path] = []
    count = 0
    total = 0

    def accept(relative: Path, size: int) -> Path | None:
        nonlocal count, total
        suffix = relative.suffix.lower()
        if suffix not in IMAGE_SUFFIXES and relative.name.casefold() != "asset_manifest.json":
            return None
        count += 1
        total += size
        if count > MAX_FILES or total > MAX_UNCOMPRESSED_BYTES:
            raise BridgeError("asset package exceeds 500 files or 2 GB uncompressed")
        destination = assets_root / relative
        if relative.name.casefold() == "asset_manifest.json":
            manifests.append(destination)
        return destination

    for raw in inputs:
        source = raw.expanduser().resolve()
        if source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    destination = accept(path.relative_to(source), path.stat().st_size)
                    if destination:
                        _copy_unique(path, destination, seen)
        elif source.is_file() and source.suffix.lower() == ".zip":
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    relative = Path(*_safe_zip_member(info).parts)
                    destination = accept(relative, info.file_size)
                    if destination:
                        with archive.open(info) as stream:
                            _copy_unique(stream, destination, seen)
        elif source.is_file():
            destination = accept(Path(source.name), source.stat().st_size)
            if not destination:
                raise BridgeError(f"unsupported asset file: {source.name}")
            _copy_unique(source, destination, seen)
        else:
            raise BridgeError(f"asset input does not exist: {source}")
    if len(manifests) > 1:
        raise BridgeError("asset package contains more than one asset_manifest.json")
    return manifests[0] if manifests else None


def _write_manifest(bundle: Path, packaged_manifest: Path | None) -> None:
    assets = (bundle / "assets").resolve()
    normalized: dict[str, str] = {}
    if packaged_manifest:
        try:
            value = json.loads(packaged_manifest.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise BridgeError("asset_manifest.json must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise BridgeError("asset_manifest.json must contain an object")
        for asset_id, relative_value in value.items():
            if not isinstance(asset_id, str) or not asset_id.strip() or not isinstance(relative_value, str):
                raise BridgeError("asset_manifest.json entries must map Asset IDs to relative paths")
            relative = Path(relative_value)
            if relative.is_absolute() or ".." in relative.parts:
                raise BridgeError(f"unsafe manifest path for {asset_id}")
            candidates = [(assets / relative).resolve(), (bundle / relative).resolve()]
            path = next((item for item in candidates if item.is_file() and item.is_relative_to(assets)), None)
            if path is None:
                raise BridgeError(f"manifest asset does not exist inside the package: {asset_id}")
            normalized[asset_id] = str(path.relative_to(bundle)).replace("\\", "/")
    (bundle / "asset_manifest.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def stage_bundle(project: Path, prompts: list[Path], assets: list[Path]) -> tuple[str, list[dict], Path]:
    bundle_id = uuid4().hex
    bundle = project / "private" / "codex_uploads" / bundle_id
    prompt_root, assets_root = bundle / "prompts", bundle / "assets"
    prompt_root.mkdir(parents=True)
    assets_root.mkdir()
    files: list[dict] = []
    names: set[str] = set()
    for raw in prompts:
        path = raw.expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in PROMPT_SUFFIXES:
            raise BridgeError(f"unsupported or missing Prompt file: {path}")
        data = path.read_bytes()
        if len(data) > 5_000_000:
            raise BridgeError(f"Prompt file exceeds 5 MB: {path.name}")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise BridgeError(f"Prompt file must be UTF-8: {path.name}") from exc
        if path.name.casefold() in names:
            raise BridgeError(f"duplicate Prompt filename: {path.name}")
        names.add(path.name.casefold())
        shutil.copy2(path, prompt_root / path.name)
        files.append({"name": path.name, "text": text, "sha256": sha256(data).hexdigest()})
    if not files:
        raise BridgeError("at least one Prompt file is required")
    packaged_manifest = _collect_assets(assets, assets_root) if assets else None
    if assets and not any(path.suffix.lower() in IMAGE_SUFFIXES for path in assets_root.rglob("*")):
        raise BridgeError("asset package contains no supported images")
    _write_manifest(bundle, packaged_manifest)
    return bundle_id, files, bundle


def _request(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urlopen(request, timeout=15) as response:
            value = json.loads(response.read())
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", {})
        except (ValueError, AttributeError):
            detail = {}
        raise BridgeError(detail.get("message") or f"H3 UI returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise BridgeError("cannot reach the H3 visual service") from exc
    if isinstance(value, dict) and value.get("error"):
        raise BridgeError(value["error"].get("message", "H3 UI request failed"))
    return value


def ensure_ui(project: Path, base_url: str) -> None:
    try:
        state = _request(base_url, "GET", "/api/state")
    except BridgeError:
        pass
    else:
        if state.get("capabilities", {}).get("codex_asset_bundles") is True:
            return
        raise BridgeError("the running H3 visual service is outdated; close it and restart start-ui.cmd")
    python = project / ".venv" / "Scripts" / "python.exe"
    config = project / "private" / "config.windows.yaml"
    if not python.is_file() or not config.is_file():
        raise BridgeError("production setup is incomplete; run bootstrap")
    log_path = project / "private" / "h3-ui.log"
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    with log_path.open("ab") as log:
        subprocess.Popen(
            [str(python), "-m", "h3_worker.ui_server", "--config", str(config), "--no-open"],
            cwd=project, stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
        )
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            state = _request(base_url, "GET", "/api/state")
            if state.get("capabilities", {}).get("codex_asset_bundles") is True:
                return
        except BridgeError:
            continue
    raise BridgeError(f"H3 visual service did not start; inspect {log_path}")


def _shot_counts(preflight: dict, default: int, overrides: list[str]) -> dict[str, int]:
    if not 1 <= default <= 4:
        raise BridgeError("default generation count must be between 1 and 4")
    counts = {shot["shot_id"]: default for shot in preflight.get("shots", []) if shot.get("ready")}
    for value in overrides:
        if "=" not in value:
            raise BridgeError("shot count must use SHOT_ID=COUNT")
        shot_id, raw_count = value.rsplit("=", 1)
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise BridgeError("shot generation count must be an integer") from exc
        if shot_id not in counts or not 1 <= count <= 4:
            raise BridgeError(f"invalid shot generation count: {value}")
        counts[shot_id] = count
    return counts


def submit(args) -> dict:
    project = find_project(args.project)
    ensure_ui(project, args.ui_url)
    bundle_id, files, bundle = stage_bundle(
        project, [Path(value) for value in args.prompt], [Path(value) for value in args.asset],
    )
    payload = {"files": [{"name": item["name"], "text": item["text"]} for item in files]}
    if args.asset:
        payload["asset_bundle_id"] = bundle_id
    preflight = _request(args.ui_url, "POST", "/api/preflight", payload)
    (bundle / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    result = {"submitted": False, "bundle_id": bundle_id, "ui_url": args.ui_url, "preflight": preflight}
    if args.preflight_only:
        return result
    if not preflight.get("ready"):
        raise BridgeError("preflight failed; no generation was submitted")
    counts = _shot_counts(preflight, args.count, args.shot_count)
    started = _request(args.ui_url, "POST", "/api/runs", {
        "preflight_id": preflight["preflight_id"],
        "generation_counts": counts,
        "run_name": args.run_name,
    })
    result.update({"submitted": True, "generation_counts": counts, "run": started})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex bridge for local H3 production")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("bootstrap")
    install.add_argument("--project")
    send = commands.add_parser("submit")
    send.add_argument("--project")
    send.add_argument("--prompt", action="append", required=True)
    send.add_argument("--asset", action="append", default=[])
    send.add_argument("--run-name")
    send.add_argument("--count", type=int, default=1)
    send.add_argument("--shot-count", action="append", default=[])
    send.add_argument("--preflight-only", action="store_true")
    send.add_argument("--ui-url", default="http://127.0.0.1:8765")
    status = commands.add_parser("status")
    status.add_argument("--ui-url", default="http://127.0.0.1:8765")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            value = bootstrap(args.project)
        elif args.command == "submit":
            value = submit(args)
        else:
            value = _request(args.ui_url, "GET", "/api/state")
        _json_dump(value)
        return 0
    except (BridgeError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        _json_dump({"error": {"type": type(exc).__name__, "message": str(exc)}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
