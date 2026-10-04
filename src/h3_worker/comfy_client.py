"""Mock client and a studio-verified single-shot HTTP adapter using official routes.

https://docs.comfy.org/development/comfyui-server/comms_routes
History/upload schemas checked against Comfy-Org/ComfyUI server.py and execution.py.
"""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
import json
import mimetypes
from pathlib import Path
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .errors import ComfyClientError, CompletionTimeoutError, UnknownSubmissionError
from .models import TaskStatus


@dataclass(frozen=True)
class Completion:
    status: TaskStatus
    error: dict | None = None
    history: dict | None = None


_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".avi"}


def extract_video_output(history: dict, prompt_id: str) -> dict:
    """Return the single video artifact described by a real history response."""
    entry = history.get(prompt_id)
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    if not isinstance(outputs, dict):
        raise ComfyClientError("ComfyUI history has no output object")
    candidates = []
    for node_id, node_output in outputs.items():
        if not isinstance(node_id, str) or not isinstance(node_output, dict):
            continue
        for value in node_output.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    continue
                if Path(item["filename"]).suffix.lower() not in _VIDEO_SUFFIXES:
                    continue
                subfolder, output_type = item.get("subfolder", ""), item.get("type")
                if not isinstance(subfolder, str) or not isinstance(output_type, str):
                    raise ComfyClientError("Unsupported video output schema")
                candidates.append({
                    "node_id": node_id,
                    "filename": item["filename"],
                    "subfolder": subfolder,
                    "type": output_type,
                })
    if len(candidates) != 1:
        raise ComfyClientError("Expected exactly one video output in ComfyUI history")
    return candidates[0]


class ComfyClient(ABC):
    @abstractmethod
    def upload_image(self, path: Path) -> str:
        """Return the server-side image name, including its subfolder."""

    @abstractmethod
    def submit_prompt(self, workflow: dict, *, shot_id: str) -> str:
        """Return a prompt_id. shot_id is mock failure-injection context."""

    @abstractmethod
    def get_queue(self) -> dict:
        pass

    @abstractmethod
    def get_history(self, prompt_id: str) -> dict:
        pass

    def wait_for_idle(self, *, timeout: float = 3600, poll_interval: float = 1.0) -> None:
        if timeout <= 0 or poll_interval <= 0:
            raise ComfyClientError("Wait timeout and poll interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            queue = self.get_queue()
            if not queue["queue_running"] and not queue["queue_pending"]:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CompletionTimeoutError("Timed out waiting for ComfyUI queue to become idle")
            time.sleep(min(poll_interval, remaining))

    def wait_for_completion(
        self,
        prompt_id: str,
        *,
        timeout: float = 3600,
        poll_interval: float = 0.05,
        on_running: Callable[[], None] | None = None,
    ) -> Completion:
        if timeout <= 0 or poll_interval <= 0:
            raise ComfyClientError("Wait timeout and poll interval must be positive")
        deadline = time.monotonic() + timeout
        notified = False
        while True:
            history = self.get_history(prompt_id)
            entry = history.get(prompt_id)
            if entry is not None:
                if not isinstance(entry, dict) or not isinstance(entry.get("status"), dict):
                    raise ComfyClientError("Unsupported ComfyUI history status schema")
                status = entry["status"]
                # An execution error can have completed=false. It is still terminal.
                if status.get("status_str") == "error":
                    error = {
                        "type": "ExecutionFailedError",
                        "message": "ComfyUI task execution failed",
                    }
                    messages = status.get("messages", [])
                    execution_error = next(
                        (value for name, value in reversed(messages)
                         if name == "execution_error" and isinstance(value, dict)),
                        None,
                    ) if isinstance(messages, list) else None
                    if execution_error is not None:
                        allowed = {
                            key: execution_error[key]
                            for key in (
                                "node_id", "node_type", "exception_type", "exception_message",
                            )
                            if isinstance(execution_error.get(key), (str, int, float, bool))
                        }
                        if allowed:
                            error["details"] = allowed
                    return Completion(TaskStatus.FAILED, error, history)
                if status.get("status_str") == "success" and status.get("completed") is True:
                    return Completion(TaskStatus.SUCCESS, history=history)
                if status.get("status_str") not in {"success", "error"}:
                    raise ComfyClientError("Unsupported ComfyUI history status schema")
            queue = self.get_queue()
            for item in queue.get("queue_running", []):
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    raise ComfyClientError("Unsupported ComfyUI queue schema")
                if item[1] == prompt_id and not notified:
                    if on_running is not None:
                        on_running()
                    notified = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CompletionTimeoutError("Timed out waiting for ComfyUI completion")
            time.sleep(min(poll_interval, remaining))


class MockComfyClient(ComfyClient):
    """Lazy clock-driven FIFO queue; no worker threads, GPU or network."""

    def __init__(self, *, runtime_seconds: float = 0.1, failures: dict[str, int] | None = None):
        if not 0.1 <= runtime_seconds <= 0.5:
            raise ComfyClientError("Mock runtime must be between 0.1 and 0.5 seconds")
        self.runtime_seconds = runtime_seconds
        self.failures = dict(failures or {})
        if any(type(value) is not int or value < -1 for value in self.failures.values()):
            raise ComfyClientError("Mock failure counts must be -1 (always) or non-negative")
        self.jobs: dict[str, dict] = {}
        self.attempt_counts: dict[str, int] = {}
        self.uploads: dict[str, bytes] = {}

    def upload_image(self, path: Path) -> str:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ComfyClientError("Cannot read image for mock upload") from exc
        # Unique names keep equally-named local assets distinct.
        name = f"mock/{uuid4().hex}{path.suffix.lower()}"
        self.uploads[name] = content
        return name

    def submit_prompt(self, workflow: dict, *, shot_id: str) -> str:
        prompt_id = str(uuid4())
        attempt = self.attempt_counts.get(shot_id, 0) + 1
        self.attempt_counts[shot_id] = attempt
        count = self.failures.get(shot_id, 0)
        self.jobs[prompt_id] = {
            "workflow": deepcopy(workflow),
            "shot_id": shot_id,
            "status": TaskStatus.PENDING,
            "queued_at": time.monotonic(),
            "started_at": None,
            "fail": count == -1 or attempt <= count,
        }
        return prompt_id

    def _refresh(self) -> None:
        now = time.monotonic()
        for job in self.jobs.values():
            if job["status"] == TaskStatus.RUNNING:
                if now - job["started_at"] >= self.runtime_seconds:
                    job["status"] = TaskStatus.FAILED if job["fail"] else TaskStatus.SUCCESS
                else:
                    return
        for job in self.jobs.values():
            if job["status"] == TaskStatus.PENDING:
                if now - job["queued_at"] >= 0.01:
                    job["started_at"] = now
                    job["status"] = TaskStatus.RUNNING
                return

    def get_queue(self) -> dict:
        self._refresh()
        return {
            "queue_running": [[i, pid] for i, (pid, job) in enumerate(self.jobs.items())
                              if job["status"] == TaskStatus.RUNNING],
            "queue_pending": [[i, pid] for i, (pid, job) in enumerate(self.jobs.items())
                              if job["status"] == TaskStatus.PENDING],
        }

    def get_history(self, prompt_id: str) -> dict:
        self._refresh()
        job = self.jobs.get(prompt_id)
        if job is None or job["status"] not in {TaskStatus.SUCCESS, TaskStatus.FAILED}:
            return {}
        success = job["status"] == TaskStatus.SUCCESS
        return {prompt_id: {"status": {
            "status_str": "success" if success else "error", "completed": success,
        }, "outputs": {}}}


class RealComfyClient(ComfyClient):
    """Self-hosted ComfyUI HTTP adapter for guarded single-shot integration."""

    def __init__(self, base_url: str, *, request_timeout: float = 30):
        try:
            url = urlsplit(base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError
            url.port
        except (ValueError, TypeError) as exc:
            raise ComfyClientError("Invalid ComfyUI base URL") from exc
        if request_timeout <= 0:
            raise ComfyClientError("HTTP request timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.client_id = str(uuid4())
        self.repair_events = []

    def _request(
        self,
        route: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
        retryable: bool = False,
        ambiguous_submission: bool = False,
    ) -> dict:
        result = None
        for attempt in range(1, 4):
            request = Request(self.base_url + route, data=data, headers={"Content-Type": content_type})
            try:
                with urlopen(request, timeout=self.request_timeout) as response:
                    result = json.load(response)
                break
            except HTTPError as exc:
                if ambiguous_submission and exc.code >= 500:
                    raise UnknownSubmissionError(
                        "Prompt submission response is unknown; automatic resubmission is disabled"
                    ) from None
                if retryable and exc.code >= 500 and attempt < 3:
                    self.repair_events.append({
                        "type": "http_retry", "operation": route.split("/")[1],
                        "attempt": attempt + 1, "status": exc.code,
                    })
                    time.sleep((0.2, 0.5)[attempt - 1])
                    continue
                raise ComfyClientError(
                    f"ComfyUI HTTP request failed (status {exc.code})"
                ) from None
            except (URLError, OSError, ValueError):
                if ambiguous_submission:
                    raise UnknownSubmissionError(
                        "Prompt submission response is unknown; automatic resubmission is disabled"
                    ) from None
                if retryable and attempt < 3:
                    self.repair_events.append({
                        "type": "transport_retry", "operation": route.split("/")[1],
                        "attempt": attempt + 1,
                    })
                    time.sleep((0.2, 0.5)[attempt - 1])
                    continue
                raise ComfyClientError("ComfyUI transport or JSON response error") from None
        if not isinstance(result, dict):
            raise ComfyClientError("ComfyUI response must be an object")
        return result

    def upload_image(self, path: Path) -> str:
        boundary = uuid4().hex
        # Use a generated ASCII filename rather than exposing a local basename/path.
        filename = uuid4().hex + path.suffix.lower()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        content = None
        for attempt in range(1, 4):
            try:
                content = path.read_bytes()
                break
            except OSError as exc:
                if attempt == 3:
                    raise ComfyClientError("Cannot read upload image") from exc
                self.repair_events.append({
                    "type": "asset_read_retry", "file": path.name, "attempt": attempt + 1,
                })
                time.sleep((0.2, 0.5)[attempt - 1])
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self._request(
            "/upload/image", data=body,
            content_type=f"multipart/form-data; boundary={boundary}", retryable=True,
        )
        name, folder = result.get("name"), result.get("subfolder", "")
        if (not isinstance(name, str) or not name or not isinstance(folder, str)
                or result.get("type") != "input"):
            raise ComfyClientError("Unsupported image upload response schema")
        return f"{folder}/{name}" if folder else name

    def submit_prompt(self, workflow: dict, *, shot_id: str) -> str:
        result = self._request(
            "/prompt",
            data=json.dumps({"prompt": workflow, "client_id": self.client_id}).encode("utf-8"),
            ambiguous_submission=True,
        )
        prompt_id = result.get("prompt_id")
        if result.get("error") or not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyClientError("ComfyUI did not accept the workflow")
        return prompt_id

    def get_queue(self) -> dict:
        result = self._request("/queue", retryable=True)
        if not all(isinstance(result.get(key), list) for key in ("queue_running", "queue_pending")):
            raise ComfyClientError("Unsupported ComfyUI queue schema")
        return result

    def get_history(self, prompt_id: str) -> dict:
        return self._request("/history/" + quote(prompt_id, safe=""), retryable=True)

    def get_system_stats(self) -> dict:
        return self._request("/system_stats", retryable=True)

    def get_h3_reference_limit(self) -> int:
        result = self._request(
            "/object_info/MiniMaxH3ReferenceToVideo", retryable=True,
        )
        try:
            maximum = result["MiniMaxH3ReferenceToVideo"]["input"]["optional"]["ref_images"][1][
                "template"
            ]["max"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ComfyClientError("Unsupported MiniMax H3 object_info schema") from exc
        if type(maximum) is not int or not 0 <= maximum <= 9:
            raise ComfyClientError("Unsupported MiniMax H3 reference image limit")
        return maximum

    def wait_for_idle(self, *, timeout: float = 3600, poll_interval: float = 1.0) -> None:
        queue = self.get_queue()
        busy = bool(queue["queue_running"] or queue["queue_pending"])
        if not busy:
            return
        self.repair_events.append({"type": "queue_wait", "status": "started"})
        super().wait_for_idle(timeout=timeout, poll_interval=poll_interval)
        self.repair_events.append({"type": "queue_wait", "status": "completed"})

    def wait_for_completion(
        self,
        prompt_id: str,
        *,
        timeout: float = 3600,
        poll_interval: float = 1.0,
        on_running: Callable[[], None] | None = None,
    ) -> Completion:
        return super().wait_for_completion(
            prompt_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_running=on_running,
        )
