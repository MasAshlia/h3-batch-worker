from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from .comfy_client import ComfyClient
from .errors import ConfigurationError, WorkerError
from .models import BatchReport, TaskStatus
from .workflow_patcher import WorkflowPatcher

_LOG = logging.getLogger("h3_worker.queue")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueManager:
    def __init__(self, client: ComfyClient, patcher: WorkflowPatcher, output_root: Path):
        self.client, self.patcher, self.output_root = client, patcher, output_root

    def run(self, report: BatchReport) -> tuple[Path, dict]:
        if not report.ready:
            raise ConfigurationError("BATCH NOT READY: all tasks must pass preflight")
        run_dir = self.output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:12])
        run_dir.mkdir(parents=True)
        run_path = run_dir / "run.json"
        record = {
            "batch_id": run_dir.name, "episode": report.episode,
            "started_at": _now(), "finished_at": None,
            "shots": [{
                "shot_id": compiled.task.shot_id,
                "prompt": compiled.task.prompt,
                "references": compiled.task.references.copy(),
                "reference_slots": compiled.task.reference_slots,
                "duration": compiled.task.duration_seconds, "steps": compiled.task.steps,
                "seed": compiled.task.seed, "prompt_id": None,
                "status": TaskStatus.VALIDATED, "retry_count": 0,
                "started_at": None, "finished_at": None, "error": None,
                "attempts": [], "events": [],
            } for compiled in report.tasks],
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
            for compiled, shot in zip(report.tasks, record["shots"], strict=True):
                transition(compiled, shot, TaskStatus.VALIDATED)
                shot["started_at"] = _now()
                for attempt_number in range(2):
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
                        images = {slot: self.client.upload_image(path) for slot, path in compiled.assets.items()}
                        workflow = self.patcher.patch(compiled.task, images)
                        shot["prompt_id"] = self.client.submit_prompt(workflow, shot_id=shot["shot_id"])
                        attempt["prompt_id"] = shot["prompt_id"]
                        attempt["status"] = TaskStatus.SUBMITTED
                        transition(compiled, shot, TaskStatus.SUBMITTED)

                        def running():
                            attempt["status"] = TaskStatus.RUNNING
                            transition(compiled, shot, TaskStatus.RUNNING)

                        completion = self.client.wait_for_completion(shot["prompt_id"], on_running=running)
                        if completion.status not in {TaskStatus.SUCCESS, TaskStatus.FAILED}:
                            raise ConfigurationError("Completion must have a terminal status")
                        status, error = completion.status, completion.error
                    except WorkerError as exc:
                        status, error = TaskStatus.FAILED, exc.to_dict()
                    except Exception:
                        # Isolate an unexpected client failure; do not expose arbitrary exception text.
                        status = TaskStatus.FAILED
                        error = {"type": "UnexpectedClientError", "message": "Unexpected task execution error"}
                    attempt.update(status=status, error=error, finished_at=_now())
                    shot["error"] = error
                    transition(compiled, shot, status)
                    if status == TaskStatus.SUCCESS:
                        break
                shot["finished_at"] = _now()
                save()
            record["finished_at"] = _now()
            record["summary"] = {
                "success": sum(shot["status"] == TaskStatus.SUCCESS for shot in record["shots"]),
                "failed": sum(shot["status"] == TaskStatus.FAILED for shot in record["shots"]),
            }
            save()
        finally:
            _LOG.removeHandler(handler)
            handler.close()
            _LOG.setLevel(previous_level)
        return run_path, record
