from io import BytesIO
import json
import time
from urllib.error import HTTPError, URLError

import pytest

import h3_worker.comfy_client as module
from h3_worker.comfy_client import MockComfyClient, RealComfyClient
from h3_worker.errors import ComfyClientError, CompletionTimeoutError
from h3_worker.models import TaskStatus


def test_mock_pending_running_success_and_uploaded_identity(config):
    client = MockComfyClient()
    one = client.upload_image(config.asset_manifest.parent / "linmo.png")
    two = client.upload_image(config.asset_manifest.parent / "linmo.png")
    assert one != two
    assert client.uploads[one].startswith(b"\x89PNG")
    pid = client.submit_prompt({"test": {}}, shot_id="S001")
    assert client.jobs[pid]["status"] == TaskStatus.PENDING
    assert client.get_queue()["queue_pending"][0][1] == pid
    events = []
    completion = client.wait_for_completion(pid, on_running=lambda: events.append("RUNNING"))
    assert events == ["RUNNING"]
    assert completion.status == TaskStatus.SUCCESS
    assert client.get_queue() == {"queue_running": [], "queue_pending": []}
    assert client.get_history(pid)[pid]["status"]["completed"] is True


def test_mock_fifo_and_failure():
    client = MockComfyClient(failures={"S001": -1})
    first = client.submit_prompt({}, shot_id="S001")
    second = client.submit_prompt({}, shot_id="S002")
    time.sleep(0.015)
    queue = client.get_queue()
    assert queue["queue_running"] == [[0, first]]
    assert queue["queue_pending"] == [[1, second]]
    assert client.wait_for_completion(first).status == TaskStatus.FAILED
    assert client.wait_for_completion(second).status == TaskStatus.SUCCESS


def test_mock_copies_workflow():
    client = MockComfyClient()
    workflow = {"100": {"inputs": {"text": "original"}}}
    pid = client.submit_prompt(workflow, shot_id="S001")
    workflow["100"]["inputs"]["text"] = "changed"
    assert client.jobs[pid]["workflow"]["100"]["inputs"]["text"] == "original"


def test_wait_timeout():
    with pytest.raises(CompletionTimeoutError):
        MockComfyClient().wait_for_completion("unknown", timeout=0.02, poll_interval=0.01)


def test_http_routes_and_payload_without_network(monkeypatch, config):
    calls = []

    def transport(request, timeout):
        calls.append(request)
        assert timeout == 30
        route = request.full_url.removeprefix("http://127.0.0.1:8188")
        responses = {
            "/upload/image": {"name": "server.png", "subfolder": "h3", "type": "input"},
            "/prompt": {"prompt_id": "test-pid"},
            "/queue": {"queue_running": [], "queue_pending": []},
            "/history/test-pid": {"test-pid": {"status": {"status_str": "success", "completed": True}}},
        }
        return BytesIO(json.dumps(responses[route]).encode())

    monkeypatch.setattr(module, "urlopen", transport)
    client = RealComfyClient("http://127.0.0.1:8188")
    assert client.upload_image(config.asset_manifest.parent / "linmo.png") == "h3/server.png"
    assert b'name="image"' in calls[0].data
    assert b'name="type"\r\n\r\ninput' in calls[0].data
    assert b"\x89PNG" in calls[0].data
    workflow = {"100": {"inputs": {"text": "测试"}}}
    pid = client.submit_prompt(workflow, shot_id="S001")
    assert json.loads(calls[-1].data)["prompt"] == workflow
    assert client.get_queue()["queue_pending"] == []
    assert client.wait_for_completion(pid).status == TaskStatus.SUCCESS
    assert [call.get_method() for call in calls] == ["POST", "POST", "GET", "GET"]


def test_history_failure_with_completed_false_is_terminal(monkeypatch):
    client = RealComfyClient("http://localhost:8188")
    monkeypatch.setattr(client, "get_history", lambda pid: {
        pid: {"status": {"status_str": "error", "completed": False}},
    })
    assert client.wait_for_completion("pid").status == TaskStatus.FAILED


def test_history_incomplete_success_does_not_claim_completion(monkeypatch):
    client = RealComfyClient("http://localhost:8188")
    monkeypatch.setattr(client, "get_history", lambda pid: {
        pid: {"status": {"status_str": "success", "completed": False}},
    })
    monkeypatch.setattr(client, "get_queue", lambda: {"queue_running": [], "queue_pending": []})
    with pytest.raises(CompletionTimeoutError):
        client.wait_for_completion("pid", timeout=0.02, poll_interval=0.01)


@pytest.mark.parametrize("result", [[], {}, {"pid": {"status": {"status_str": "unknown"}}}])
def test_unknown_http_schemas_rejected(monkeypatch, result):
    client = RealComfyClient("http://localhost:8188")
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: BytesIO(json.dumps(result).encode()))
    with pytest.raises(ComfyClientError):
        if isinstance(result, dict) and "pid" in result:
            client.wait_for_completion("pid")
        else:
            client.get_queue()


@pytest.mark.parametrize("exception", [
    URLError("secret-token /Users/private/secret"),
    HTTPError("http://private/secret", 403, "secret-token", {}, None),
])
def test_transport_errors_do_not_leak_secrets(monkeypatch, exception):
    def transport(*args, **kwargs):
        raise exception

    monkeypatch.setattr(module, "urlopen", transport)
    with pytest.raises(ComfyClientError) as captured:
        RealComfyClient("http://localhost:8188").get_queue()
    assert "secret" not in str(captured.value)
    assert "/Users" not in str(captured.value)


@pytest.mark.parametrize("url", ["file:///tmp", "http://user:password@localhost:8188", "http://localhost:bad"])
def test_invalid_or_credentialed_url_rejected(url):
    with pytest.raises(ComfyClientError):
        RealComfyClient(url)
