import json

import pytest

import h3_worker.queue_manager as queue_module
from h3_worker.comfy_client import Completion, MockComfyClient
from h3_worker.errors import ConfigurationError, UnknownSubmissionError
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
    assert statuses == [
        "VALIDATED", "UPLOADING", "SUBMITTED", "RUNNING", "FAILED",
        "RETRY", "UPLOADING", "SUBMITTED", "RUNNING", "FAILED",
    ]
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


def test_run_record_replace_retries_windows_sharing_violation(batch, tmp_path, monkeypatch):
    report, patcher = batch
    report.items = report.items[:1]
    original_replace = queue_module.Path.replace
    calls = 0

    def temporarily_locked(path, target):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise PermissionError("file is temporarily in use")
        return original_replace(path, target)

    monkeypatch.setattr(queue_module.Path, "replace", temporarily_locked)
    _, record = QueueManager(MockComfyClient(), patcher, tmp_path, max_attempts=1).run(report)
    assert calls > 2
    assert record["shots"][0]["status"] == TaskStatus.SUCCESS


def test_unexpected_monitor_error_after_prompt_id_is_unknown_not_failed(batch, tmp_path):
    report, patcher = batch
    report.items = report.items[:1]

    class BrokenMonitorClient(MockComfyClient):
        def wait_for_completion(self, prompt_id, **kwargs):
            kwargs["on_running"]()
            raise RuntimeError("private monitor detail")

    client = BrokenMonitorClient()
    run_path, record = QueueManager(
        client, patcher, tmp_path, max_attempts=2, stop_on_failure=True,
    ).run(report)
    shot = record["shots"][0]
    assert len(client.jobs) == 1
    assert shot["status"] == TaskStatus.UNKNOWN_SUBMISSION
    assert shot["error"] == {
        "type": "MonitoringStateUnknownError",
        "message": "Prompt was submitted but monitoring failed; automatic resubmission is disabled",
        "details": {"exception_type": "RuntimeError"},
    }
    assert "private monitor detail" not in run_path.read_text(encoding="utf-8")


def test_real_mode_records_history_video_and_shot_mapping(batch, tmp_path):
    report, patcher = batch
    report.items = report.items[:1]

    class OutputClient(MockComfyClient):
        def wait_for_completion(self, prompt_id, **kwargs):
            kwargs["on_running"]()
            return Completion(TaskStatus.SUCCESS, history={prompt_id: {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"92": {"images": [{
                    "filename": "REAL_TEST_S001.mp4", "subfolder": "", "type": "output",
                }]}},
            }})

    (tmp_path / "REAL_TEST_S001.mp4").write_bytes(b"video")
    debug = tmp_path / "debug"
    _, record = QueueManager(
        OutputClient(), patcher, tmp_path / "runs",
        max_attempts=1,
        require_queue_confirmation=True,
        require_video_output=True,
        debug_root=debug,
        comfy_output=tmp_path,
    ).run(report)
    shot = record["shots"][0]
    assert shot["output"] == {
        "node_id": "92", "filename": "REAL_TEST_S001.mp4", "subfolder": "", "type": "output",
    }
    assert shot["submitted_at"] and shot["running_at"] and shot["completed_at"]
    assert (debug / "history_S001.json").is_file()
    assert (next((tmp_path / "runs").iterdir()) / "history_S001.json").is_file()


def test_real_sequence_waits_for_an_interleaved_job(batch, tmp_path):
    report, patcher = batch
    report.items = report.items[:2]

    class InterleavedClient(MockComfyClient):
        completed = 0
        busy_checks = 0

        def wait_for_completion(self, prompt_id, **kwargs):
            self.completed += 1
            return Completion(TaskStatus.SUCCESS)

        def get_queue(self):
            if self.completed == 1 and self.busy_checks == 0:
                self.busy_checks += 1
                return {"queue_running": [[0, "external"]], "queue_pending": []}
            return {"queue_running": [], "queue_pending": []}

    client = InterleavedClient()
    _, record = QueueManager(
        client,
        patcher,
        tmp_path,
        max_attempts=1,
        require_idle_before_each=True,
        stop_on_failure=True,
    ).run(report)
    assert client.busy_checks == 1
    assert record["summary"] == {"success": 2, "failed": 0}


def test_unknown_submission_is_isolated_without_second_submit(batch, tmp_path):
    report, patcher = batch
    report.items = report.items[:2]

    class UnknownClient(MockComfyClient):
        submits = 0

        def submit_prompt(self, workflow, *, shot_id):
            self.submits += 1
            raise UnknownSubmissionError("response unknown")

    client = UnknownClient()
    _, record = QueueManager(
        client, patcher, tmp_path, max_attempts=2, stop_on_failure=True,
    ).run(report)
    assert client.submits == 1
    assert len(record["shots"][0]["attempts"]) == 1
    assert record["shots"][0]["status"] == TaskStatus.UNKNOWN_SUBMISSION
    assert record["shots"][1]["status"] == TaskStatus.SKIPPED
    assert record["summary"] == {"success": 0, "failed": 1}


def test_generation_counts_expand_each_shot_in_strict_order(batch, tmp_path):
    report, patcher = batch
    report.items = report.items[:2]
    client = MockComfyClient()
    _, record = QueueManager(client, patcher, tmp_path, max_attempts=1).run(
        report, generation_counts={"S001": 2, "S002": 3},
    )
    assert [(shot["shot_id"], shot["generation_index"]) for shot in record["shots"]] == [
        ("S001", 1), ("S001", 2), ("S002", 1), ("S002", 2), ("S002", 3),
    ]
    assert [job["shot_id"] for job in client.jobs.values()] == [
        "S001", "S001", "S002", "S002", "S002",
    ]
    assert len({shot["prompt_id"] for shot in record["shots"]}) == 5
    assert [shot["output_label"] for shot in record["shots"]] == [
        "EP_TEST_S001_V01", "EP_TEST_S001_V02", "EP_TEST_S002_V01",
        "EP_TEST_S002_V02", "EP_TEST_S002_V03",
    ]


@pytest.mark.parametrize("counts", [
    {"S001": 0, "S002": 1}, {"S001": 1}, {"S001": True, "S002": 1},
])
def test_generation_counts_are_complete_integers_from_one_to_four(batch, tmp_path, counts):
    report, patcher = batch
    report.items = report.items[:2]
    with pytest.raises(ConfigurationError):
        QueueManager(MockComfyClient(), patcher, tmp_path).run(
            report, generation_counts=counts,
        )


def test_successful_history_waits_briefly_for_delayed_output(batch, tmp_path, monkeypatch):
    report, patcher = batch
    report.items = report.items[:1]
    output = tmp_path / "delayed.mp4"

    class DelayedOutputClient(MockComfyClient):
        def wait_for_completion(self, prompt_id, **kwargs):
            return Completion(TaskStatus.SUCCESS, history={prompt_id: {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"92": {"images": [{
                    "filename": output.name, "subfolder": "", "type": "output",
                }]}},
            }})

    monkeypatch.setattr(queue_module.time, "sleep", lambda _: output.write_bytes(b"video"))
    _, record = QueueManager(
        DelayedOutputClient(), patcher, tmp_path / "runs",
        max_attempts=1, require_video_output=True, comfy_output=tmp_path,
        output_wait_seconds=1,
    ).run(report)
    assert record["summary"] == {"success": 1, "failed": 0}
    assert {repair["type"] for repair in record["repairs"]} == {"output_file_wait"}
