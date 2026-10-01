import json

import pytest

from h3_worker.comfy_client import MockComfyClient
from h3_worker.errors import ConfigurationError
from h3_worker.models import TaskStatus
from h3_worker.queue_manager import QueueManager


def test_twenty_shots_third_fails_twice_fourth_and_rest_continue(batch, tmp_path):
    report, patcher = batch
    client = MockComfyClient(failures={"S003": -1})
    run_path, record = QueueManager(client, patcher, tmp_path).run(report)
    assert record["summary"] == {"success": 19, "failed": 1}
    assert json.loads(run_path.read_text(encoding="utf-8")) == record
    third, fourth = record["shots"][2:4]
    assert third["status"] == TaskStatus.FAILED
    assert third["retry_count"] == 1
    assert len(third["attempts"]) == 2
    assert third["attempts"][0]["prompt_id"] != third["attempts"][1]["prompt_id"]
    assert fourth["status"] == TaskStatus.SUCCESS
    assert third["finished_at"] <= fourth["started_at"]
    statuses = [event["status"] for event in third["events"]]
    assert statuses == ["VALIDATED", "SUBMITTED", "RUNNING", "FAILED", "RETRY", "SUBMITTED", "RUNNING", "FAILED"]
    assert client.attempt_counts["S003"] == 2
    assert len(client.jobs) == 21
    for shot in record["shots"]:
        assert all(key in shot for key in ("shot_id", "prompt", "references", "duration", "steps",
                                           "prompt_id", "status", "retry_count", "started_at", "finished_at", "error"))
        assert shot["started_at"] is not None and shot["finished_at"] is not None
    log = (run_path.parent / "run.log").read_text(encoding="utf-8")
    assert "S003 RETRY" in log
    assert "S004 SUCCESS" in log
    assert str(tmp_path) not in log
    assert report.tasks[0].task.prompt not in log
    assert "林默" not in log
    # Submitted workflow contains uploaded server image names, never local paths.
    workflow = next(iter(client.jobs.values()))["workflow"]
    assert workflow["200"]["inputs"]["ref_image_0"].startswith("mock/")


def test_transient_failure_retries_once_and_clears_final_error(batch, tmp_path):
    report, patcher = batch
    report.items = report.items[:4]
    client = MockComfyClient(failures={"S003": 1})
    _, record = QueueManager(client, patcher, tmp_path).run(report)
    assert record["summary"] == {"success": 4, "failed": 0}
    third = record["shots"][2]
    assert third["retry_count"] == 1
    assert third["error"] is None
    assert third["attempts"][0]["error"]["type"] == "ExecutionFailedError"
    assert third["attempts"][1]["status"] == "SUCCESS"


def test_invalid_batch_never_submits_valid_subset(batch, tmp_path):
    report, patcher = batch
    report.items[2].compiled = None
    report.items[2].errors = [{"type": "MissingAssetError", "message": "Missing asset"}]
    client = MockComfyClient()
    with pytest.raises(ConfigurationError):
        QueueManager(client, patcher, tmp_path).run(report)
    assert client.jobs == {}
    assert client.uploads == {}
    assert list(tmp_path.iterdir()) == []


def test_upload_exception_does_not_stop_next_shot_or_leak_error(batch, tmp_path, monkeypatch):
    report, patcher = batch
    report.items = report.items[:2]
    client = MockComfyClient()
    upload = client.upload_image
    calls = 0

    def flaky(path):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OSError("password secret-token /Users/private")
        return upload(path)

    monkeypatch.setattr(client, "upload_image", flaky)
    path, record = QueueManager(client, patcher, tmp_path).run(report)
    assert record["summary"] == {"success": 1, "failed": 1}
    assert record["shots"][0]["prompt_id"] is None
    assert record["shots"][1]["status"] == "SUCCESS"
    assert "secret-token" not in path.read_text(encoding="utf-8")
