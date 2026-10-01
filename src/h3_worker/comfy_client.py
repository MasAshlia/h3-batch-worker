"""Mock client and an unverified-on-studio HTTP adapter using official routes.

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

from .errors import ComfyClientError, CompletionTimeoutError
from .models import TaskStatus


@dataclass(frozen=True)
class Completion:
    status: TaskStatus
    error: dict | None = None


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
                    return Completion(TaskStatus.FAILED, {
                        "type": "ExecutionFailedError",
                        "message": "ComfyUI task execution failed",
                    })
                if status.get("status_str") == "success" and status.get("completed") is True:
                    return Completion(TaskStatus.SUCCESS)
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
    """Basic self-hosted HTTP adapter. Studio integration is NOT verified in V0.1.

    V0.1 CLI deliberately requires --mock. No authentication or video download here.
    """

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

    def _request(self, route: str, *, data: bytes | None = None, content_type: str = "application/json") -> dict:
        request = Request(self.base_url + route, data=data, headers={"Content-Type": content_type})
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            # Raw server bodies/URLs can contain secrets or local studio paths.
            raise ComfyClientError(f"ComfyUI HTTP request failed (status {exc.code})") from None
        except (URLError, OSError, ValueError) as exc:
            raise ComfyClientError("ComfyUI transport or JSON response error") from None
        if not isinstance(result, dict):
            raise ComfyClientError("ComfyUI response must be an object")
        return result

    def upload_image(self, path: Path) -> str:
        boundary = uuid4().hex
        # Use a generated ASCII filename rather than exposing a local basename/path.
        filename = uuid4().hex + path.suffix.lower()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ComfyClientError("Cannot read upload image") from exc
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self._request("/upload/image", data=body,
                               content_type=f"multipart/form-data; boundary={boundary}")
        name, folder = result.get("name"), result.get("subfolder", "")
        if (not isinstance(name, str) or not name or not isinstance(folder, str)
                or result.get("type") != "input"):
            raise ComfyClientError("Unsupported image upload response schema")
        return f"{folder}/{name}" if folder else name

    def submit_prompt(self, workflow: dict, *, shot_id: str) -> str:
        result = self._request("/prompt", data=json.dumps({
            "prompt": workflow, "client_id": self.client_id,
        }).encode("utf-8"))
        prompt_id = result.get("prompt_id")
        if result.get("error") or not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyClientError("ComfyUI did not accept the workflow")
        return prompt_id

    def get_queue(self) -> dict:
        result = self._request("/queue")
        if not all(isinstance(result.get(key), list) for key in ("queue_running", "queue_pending")):
            raise ComfyClientError("Unsupported ComfyUI queue schema")
        return result

    def get_history(self, prompt_id: str) -> dict:
        return self._request("/history/" + quote(prompt_id, safe=""))
