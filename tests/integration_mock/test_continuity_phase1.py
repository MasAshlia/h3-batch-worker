import json
from pathlib import Path
from dataclasses import replace

import pytest

from h3_worker.comfy_client import Completion, MockComfyClient
from h3_worker.errors import ComfyClientError
from h3_worker.models import TaskStatus
from h3_worker.queue_manager import QueueManager
from h3_worker.workflow_patcher import WorkflowPatcher
from h3_worker.parser import parse_prompt


def _template():
    return {
        "1": {"class_type": "Prompt", "inputs": {"text": ""}},
        "2": {"class_type": "Duration", "inputs": {"value": 5.0}},
        "3": {"class_type": "Scheduler", "inputs": {"steps": 15}},
        "4": {"class_type": "Refs", "inputs": {"ref_image_0": "", "ref_image_1": ""}},
        "5": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "x"}},
    }


def _mapping():
    return {"prompt_node": "1", "reference_inputs": {
                "ref_image_0": {"node": "4", "input": "ref_image_0"},
                "ref_image_1": {"node": "4", "input": "ref_image_1"},
            }, "duration_node": "2", "scheduler_node": "3",
            "continuity_input": {"node": "4", "input": "ref_image_1"}}


def _report(tmp_path):
    from h3_worker.models import BatchItem, BatchReport, CompiledTask
    tasks = []
    for sid in ("S001", "S002"):
        p = tmp_path / f"{sid}.png"; p.write_bytes(b"x")
        t = parse_prompt(sid, "duration: 5\n@image_1: asset\n\nbody")
        tasks.append(BatchItem(sid, CompiledTask(t, {"ref_image_0": p}, {}, source_path=p)))
    return BatchReport("EP", tasks)


def test_continuity_mapping_is_configurable():
    patcher = WorkflowPatcher(_template(), _mapping())
    task = parse_prompt("S001", "duration: 5\n@image_1: asset\n\nbody")
    workflow, _ = patcher.patch_with_mapping(task, {"ref_image_0": "mock/a.png"}, continuity_value="mock/tail.png")
    assert workflow["4"]["inputs"]["ref_image_1"] == "mock/tail.png"


def test_queue_orders_and_records_boundary(monkeypatch, tmp_path):
    report = _report(tmp_path)
    patcher = WorkflowPatcher(_template(), _mapping())
    client = MockComfyClient()
    outputs = iter(["a.mp4", "b.mp4"])
    def fake_extract(*args, **kwargs):
        target = Path(kwargs["boundary_dir"]) / "tail.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"frame")
        return target

    monkeypatch.setattr("h3_worker.queue_manager._extract_last_frame", fake_extract)
    monkeypatch.setattr("h3_worker.queue_manager.extract_video_output", lambda *args: {"node_id":"5", "filename":next(outputs), "subfolder":"", "type":"output"})
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).write_bytes(b"v")
    _, record = QueueManager(client, patcher, tmp_path / "runs", require_video_output=True, comfy_output=tmp_path).run(report)
    assert [j["shot_id"] for j in client.jobs.values()] == ["S001", "S002"]
    assert record["shots"][0]["boundary"]["mode"] == "last_frame"
    assert record["shots"][1]["boundary"]["source"] == "S001"
    assert record["shots"][1]["boundary"]["uploaded_server_filename"]


def test_boundary_extraction_failure_fails_episode(monkeypatch, tmp_path):
    report = _report(tmp_path)
    patcher = WorkflowPatcher(_template(), _mapping())
    client = MockComfyClient()
    monkeypatch.setattr(
        "h3_worker.queue_manager._extract_last_frame",
        lambda *a, **k: (_ for _ in ()).throw(ComfyClientError("ffmpeg failed")),
    )
    monkeypatch.setattr("h3_worker.queue_manager.extract_video_output", lambda *args: {"node_id":"5", "filename":"a.mp4", "subfolder":"", "type":"output"})
    (tmp_path / "a.mp4").write_bytes(b"v")
    _, record = QueueManager(client, patcher, tmp_path / "runs", require_video_output=True, comfy_output=tmp_path).run(report)
    assert record["summary"] == {"success": 0, "failed": 1}
    assert record["shots"][0]["status"] == TaskStatus.FAILED
    assert record["shots"][1]["status"] == TaskStatus.SKIPPED
