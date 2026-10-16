from datetime import datetime, timezone
from dataclasses import replace
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import secrets
import time
import shutil
import subprocess
from typing import Callable
from uuid import uuid4

from .comfy_client import ComfyClient, extract_video_output
from .errors import ComfyClientError, ConfigurationError, UnknownSubmissionError, WorkerError
from .models import BatchReport, TaskStatus
from .workflow_patcher import WorkflowPatcher

_LOG = logging.getLogger("h3_worker.queue")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_last_frame(video_path: Path, *, boundary_dir: Path, executable: str = "ffmpeg") -> Path:
    if shutil.which(executable) is None:
        raise ComfyClientError("Configured ffmpeg executable is unavailable")
    boundary_dir.mkdir(parents=True, exist_ok=True)
    target = boundary_dir / "last_frame.png"
    result = subprocess.run([executable, "-y", "-sseof", "-0.1", "-i", str(video_path), "-frames:v", "1", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ComfyClientError("Cannot extract final video frame")
    return target


class QueueManager:
    def __init__(
        self,
        client: ComfyClient,
        patcher: WorkflowPatcher,
        output_root: Path,
        *,
        max_attempts: int = 2,
        require_queue_confirmation: bool = False,
        require_video_output: bool = False,
        require_idle_before_each: bool = False,
        stop_on_failure: bool = False,
        output_wait_seconds: float = 60,
        debug_root: Path | None = None,
        comfy_output: Path | None = None,
        on_run_created: Callable[[Path], None] | None = None,
        ffmpeg_executable: str = "ffmpeg",
    ):
        if max_attempts not in {1, 2}:
            raise ConfigurationError("max_attempts must be one or two")
        self.client, self.patcher, self.output_root = client, patcher, output_root
        self.max_attempts = max_attempts
        self.require_queue_confirmation = require_queue_confirmation
        self.require_video_output = require_video_output
        self.require_idle_before_each = require_idle_before_each
        self.stop_on_failure = stop_on_failure
        if output_wait_seconds < 0:
            raise ConfigurationError("output_wait_seconds cannot be negative")
        self.output_wait_seconds = output_wait_seconds
        self.debug_root = debug_root
        self.comfy_output = comfy_output
        self.on_run_created = on_run_created
        self.ffmpeg_executable = ffmpeg_executable

    def run(
        self, report: BatchReport, *, generation_counts: dict[str, int] | None = None,
        run_name: str | None = None,
    ) -> tuple[Path, dict]:
        if not report.ready:
            raise ConfigurationError("BATCH NOT READY: all tasks must pass preflight")
        counts = generation_counts or {compiled.task.shot_id: 1 for compiled in report.tasks}
        expected_ids = {compiled.task.shot_id for compiled in report.tasks}
        if set(counts) != expected_ids or any(type(value) is not int or not 1 <= value <= 4
                                              for value in counts.values()):
            raise ConfigurationError("Generation counts must specify every shot with a value from one to four")
        if run_name is not None:
            if not isinstance(run_name, str) or not run_name.strip():
                raise ConfigurationError("Run name must be a non-empty string")
            run_name = run_name.strip()
            if len(run_name) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", run_name):
                raise ConfigurationError("Run name may contain letters, numbers, spaces, underscores, and hyphens")
        run_slug = re.sub(r"[^A-Za-z0-9]+", "_", run_name or report.episode).strip("_")
        run_dir = self.output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:12])
        run_dir.mkdir(parents=True)
        run_path = run_dir / "run.json"
        if self.on_run_created is not None:
            self.on_run_created(run_path)
        repair_offset = len(getattr(self.client, "repair_events", []))
        planned = [
            (compiled, generation_index, counts[compiled.task.shot_id])
            for compiled in report.tasks
            for generation_index in range(1, counts[compiled.task.shot_id] + 1)
        ]
        record = {
            "batch_id": run_dir.name, "episode": report.episode,
            "run_name": run_name or report.episode,
            "started_at": _now(), "finished_at": None,
            "repairs": [],
            "planned_generations": len(planned),
            "shots": [{
                "shot_id": compiled.task.shot_id,
                "generation_index": generation_index,
                "generation_count": generation_count,
                "output_label": (
                    f"{run_slug}_{compiled.task.shot_id}_V{generation_index:02d}"
                ),
                "prompt": compiled.task.prompt,
                "original_prompt": compiled.task.original_prompt or compiled.task.prompt,
                "original_prompt_sha256": sha256(
                    (compiled.task.original_prompt or compiled.task.prompt).encode("utf-8")
                ).hexdigest(),
                "submitted_prompt": compiled.task.prompt,
                "submitted_prompt_sha256": sha256(compiled.task.prompt.encode("utf-8")).hexdigest(),
                "source_file": compiled.source_path.name if compiled.source_path else None,
                "references": compiled.task.references.copy(),
                "reference_slots": compiled.task.reference_slots,
                "raw_duration": compiled.task.raw_duration_seconds,
                "duration": compiled.task.duration_seconds, "steps": compiled.task.steps,
                "expected_frames": compiled.task.expected_frame_count,
                "expected_output_seconds": compiled.task.expected_output_seconds,
                "transformations": compiled.task.transformations.copy(),
                "asset_mapping": compiled.asset_audit,
                "workflow_reference_mapping": compiled.workflow_mapping,
                "seed": None, "prompt_id": None,
                "status": TaskStatus.VALIDATED, "retry_count": 0,
                "started_at": None, "finished_at": None, "error": None,
                "submitted_at": None, "running_at": None, "completed_at": None,
                "output": None, "boundary": None,
                "attempts": [], "events": [],
            } for compiled, generation_index, generation_count in planned],
        }

        def save():
            temporary = run_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(run_path)

        def transition(compiled, shot, status):
            compiled.task.status = status
            shot["status"] = status
            event = {"at": _now(), "status": status, "prompt_id": shot["prompt_id"]}
            shot["events"].append(event)
            if status == TaskStatus.SUBMITTED:
                shot["submitted_at"] = event["at"]
            elif status == TaskStatus.RUNNING and shot["running_at"] is None:
                shot["running_at"] = event["at"]
            elif status in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.UNKNOWN_SUBMISSION}:
                shot["completed_at"] = event["at"]
            # No prompt, URL, exception body, token or absolute path in logs.
            _LOG.info("%s %s prompt_id=%s", shot["shot_id"], status, shot["prompt_id"] or "-")
            save()

        handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        _LOG.addHandler(handler)
        previous_level = _LOG.level
        _LOG.setLevel(logging.INFO)
        try:
            save()
            previous_boundary = None
            previous_boundary_source = None
            for plan_index, ((compiled, generation_index, _), shot) in enumerate(
                zip(planned, record["shots"], strict=True)
            ):
                transition(compiled, shot, TaskStatus.VALIDATED)
                shot["started_at"] = _now()
                if compiled.task.seed is None:
                    seed = secrets.randbits(64) if self.patcher.seed_target is not None else None
                else:
                    seed = (compiled.task.seed + generation_index - 1) % (2 ** 64)
                task = replace(compiled.task, seed=seed)
                shot["seed"] = seed
                for attempt_number in range(self.max_attempts):
                    if attempt_number:
                        shot["retry_count"] = 1
                        shot["prompt_id"] = None
                        transition(compiled, shot, TaskStatus.RETRY)
                    attempt = {"number": attempt_number + 1, "prompt_id": None,
                               "started_at": _now(), "finished_at": None,
                               "status": TaskStatus.PENDING, "error": None}
                    shot["attempts"].append(attempt)
                    save()
                    try:
                        if self.require_idle_before_each:
                            self.client.wait_for_idle()
                        transition(compiled, shot, TaskStatus.UPLOADING)
                        images = {slot: self.client.upload_image(path) for slot, path in compiled.assets.items()}
                        continuity_upload = (
                            self.client.upload_image(previous_boundary)
                            if previous_boundary is not None
                            and previous_boundary_source != shot["shot_id"]
                            else None
                        )
                        workflow, workflow_mapping = self.patcher.patch_with_mapping(
                            task, images, output_label=shot["output_label"], continuity_value=continuity_upload,
                        )
                        if continuity_upload is not None:
                            shot["boundary"] = {"source": previous_boundary_source, "extracted_path": str(previous_boundary), "uploaded_server_filename": continuity_upload, "mode": "last_frame"}
                        shot["workflow_reference_mapping"] = workflow_mapping
                        shot["prompt_id"] = self.client.submit_prompt(workflow, shot_id=shot["shot_id"])
                        attempt["prompt_id"] = shot["prompt_id"]
                        attempt["status"] = TaskStatus.SUBMITTED
                        transition(compiled, shot, TaskStatus.SUBMITTED)
                        if self.require_queue_confirmation:
                            queue = self.client.get_queue()
                            queued_ids = {
                                item[1]
                                for key in ("queue_running", "queue_pending")
                                for item in queue.get(key, [])
                                if isinstance(item, (list, tuple)) and len(item) >= 2
                            }
                            if shot["prompt_id"] not in queued_ids:
                                raise ComfyClientError("Submitted prompt_id was not visible in ComfyUI queue")

                        def running():
                            attempt["status"] = TaskStatus.RUNNING
                            transition(compiled, shot, TaskStatus.RUNNING)

                        completion = self.client.wait_for_completion(shot["prompt_id"], on_running=running)
                        if completion.status not in {TaskStatus.SUCCESS, TaskStatus.FAILED}:
                            raise ConfigurationError("Completion must have a terminal status")
                        status, error = completion.status, completion.error
                        if status == TaskStatus.SUCCESS:
                            transition(compiled, shot, TaskStatus.FINALIZING)
                        if completion.history is not None and self.debug_root is not None:
                            self.debug_root.mkdir(parents=True, exist_ok=True)
                            history_text = json.dumps(completion.history, ensure_ascii=False, indent=2)
                            (self.debug_root / f"history_{shot['shot_id']}.json").write_text(
                                history_text, encoding="utf-8",
                            )
                            (run_dir / f"history_{shot['shot_id']}.json").write_text(
                                history_text, encoding="utf-8",
                            )
                        if status == TaskStatus.SUCCESS and self.require_video_output:
                            if completion.history is None:
                                raise ComfyClientError("Successful task has no ComfyUI history")
                            output = extract_video_output(completion.history, shot["prompt_id"])
                            if self.comfy_output is None:
                                raise ConfigurationError("Real run requires paths.comfy_output")
                            output_path = self.comfy_output / output["subfolder"] / output["filename"]
                            deadline = time.monotonic() + self.output_wait_seconds
                            waited = False
                            while not output_path.is_file() or output_path.stat().st_size <= 0:
                                if time.monotonic() >= deadline:
                                    raise ComfyClientError(
                                        "History video output does not exist or is empty"
                                    )
                                waited = True
                                time.sleep(min(1, max(0, deadline - time.monotonic())))
                            if waited:
                                record["repairs"].append({
                                    "type": "output_file_wait", "shot_id": shot["shot_id"],
                                    "status": "completed",
                                })
                            shot["output"] = output
                            if self.patcher.continuity_target is not None:
                                boundary_dir = run_dir / "boundaries" / shot["shot_id"]
                                try:
                                    previous_boundary = _extract_last_frame(
                                        output_path, boundary_dir=boundary_dir,
                                        executable=self.ffmpeg_executable,
                                    )
                                except WorkerError:
                                    raise
                                except Exception as exc:
                                    raise ComfyClientError(
                                        "Cannot extract final video frame"
                                    ) from exc
                                previous_boundary_source = shot["shot_id"]
                                if shot["boundary"] is None:
                                    shot["boundary"] = {
                                        "source": shot["shot_id"],
                                        "extracted_path": str(previous_boundary),
                                        "uploaded_server_filename": None,
                                        "mode": "last_frame",
                                    }
                    except WorkerError as exc:
                        status = (
                            TaskStatus.UNKNOWN_SUBMISSION
                            if isinstance(exc, UnknownSubmissionError) else TaskStatus.FAILED
                        )
                        error = exc.to_dict()
                    except Exception:
                        # Isolate an unexpected client failure; do not expose arbitrary exception text.
                        status = TaskStatus.FAILED
                        error = {"type": "UnexpectedClientError", "message": "Unexpected task execution error"}
                    attempt.update(status=status, error=error, finished_at=_now())
                    shot["error"] = error
                    transition(compiled, shot, status)
                    if status in {TaskStatus.SUCCESS, TaskStatus.UNKNOWN_SUBMISSION}:
                        break
                shot["finished_at"] = _now()
                save()
                if (self.stop_on_failure or self.patcher.continuity_target is not None) and shot["status"] in {
                    TaskStatus.FAILED, TaskStatus.UNKNOWN_SUBMISSION,
                }:
                    for skipped in record["shots"][plan_index + 1:]:
                        skipped["status"] = TaskStatus.SKIPPED
                        skipped["finished_at"] = _now()
                        skipped["completed_at"] = skipped["finished_at"]
                        skipped["error"] = {
                            "type": "EpisodeStoppedError",
                            "message": "Skipped because an earlier generation failed",
                        }
                        skipped["events"].append({
                            "at": skipped["finished_at"], "status": TaskStatus.SKIPPED,
                            "prompt_id": None,
                        })
                    break
            record["finished_at"] = _now()
            record["summary"] = {
                "success": sum(shot["status"] == TaskStatus.SUCCESS for shot in record["shots"]),
                "failed": sum(
                    shot["status"] in {TaskStatus.FAILED, TaskStatus.UNKNOWN_SUBMISSION}
                    for shot in record["shots"]
                ),
            }
            record["repairs"].extend(
                getattr(self.client, "repair_events", [])[repair_offset:]
            )
            save()
        finally:
            _LOG.removeHandler(handler)
            handler.close()
            _LOG.setLevel(previous_level)
        return run_path, record
