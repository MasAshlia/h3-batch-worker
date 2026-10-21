from dataclasses import replace
import json
from pathlib import Path

import pytest

from h3_worker.errors import ConfigurationError
from h3_worker.ui_server import UiController, split_episode_text
import h3_worker.ui_server as ui_module


def test_master_txt_splits_only_real_shot_headers():
    text = (
        "SHOT 33-1 FIRST\nSHOT DURATION: 5.2 seconds\n"
        "A sign reads SHOT 33-9 but this is body text.\n\n"
        "SHOT 33-2 SECOND\nSHOT DURATION: 8 seconds\nBody\n"
    )
    prompts = split_episode_text(text)
    assert len(prompts) == 2
    assert prompts[0].startswith("SHOT 33-1 FIRST")
    assert prompts[1].startswith("SHOT 33-2 SECOND")


def test_master_txt_requires_duration_immediately_after_header():
    with pytest.raises(ConfigurationError):
        split_episode_text("SHOT 33-1 FIRST\nBody only\n")


def test_master_txt_accepts_duration_before_shot_and_multiple_episodes():
    text = (
        "SHOT DURATION: 5.2 seconds\n\nSHOT 33-1 - FIRST\n[UPLOAD MAPPING]\n"
        "<Picture 1> = Asset R01.\n\n[PROMPT]\nFirst.\n"
        "SHOT DURATION: 8 seconds\n\nSHOT 34-1 - SECOND\n[UPLOAD MAPPING]\n"
        "<Picture 1> = Asset R01.\n\n[PROMPT]\nSecond.\n"
    )
    prompts = split_episode_text(text)
    assert len(prompts) == 2
    assert prompts[0].startswith("SHOT DURATION: 5.2")
    assert "SHOT 34-1" in prompts[1]


def test_master_markdown_splits_short_episode_headers():
    text = (
        "## SHOT E16-01 - START\nSHOT DURATION: 7 seconds\nBody\n"
        "## SHOT E16-02 - START\nSHOT DURATION: 8 seconds\nBody\n"
    )
    prompts = split_episode_text(text)
    assert len(prompts) == 2
    assert prompts[0].startswith("## SHOT E16-01")
    assert prompts[1].startswith("## SHOT E16-02")


def test_ui_preflight_groups_multi_episode_master_without_rewriting(config, tmp_path, monkeypatch):
    asset = Path(__file__).resolve().parents[2] / "fixtures" / "assets" / "linmo.png"
    manifest = tmp_path / "assets.json"
    manifest.write_text(json.dumps({"R01": str(asset)}), encoding="utf-8")
    test_config = replace(
        config,
        asset_manifest=manifest,
        asset_roots=(asset.parent,),
        asset_index=tmp_path / "index.json",
        output_root=tmp_path / "runs",
    )

    class SchemaClient:
        def __init__(self, base_url):
            pass

        def get_h3_reference_limit(self):
            return 9

    monkeypatch.setattr(ui_module, "RealComfyClient", SchemaClient)
    text = (
        "SHOT DURATION: 5.2 seconds\n\nSHOT 33-1 - FIRST\n[UPLOAD MAPPING]\n"
        "<Picture 1> = Asset R01.\n\n[PROMPT]\nFirst.\n"
        "SHOT DURATION: 8 seconds\n\nSHOT 34-1 - SECOND\n[UPLOAD MAPPING]\n"
        "<Picture 1> = Asset R01.\n\n[PROMPT]\nSecond.\n"
    )
    result = UiController(test_config, workspace=tmp_path / "jobs").preflight([
        {"name": "master.txt", "text": text},
    ])
    assert result["ready"] is True
    assert result["episode"] == "EP33–EP34"
    assert result["episodes"] == ["EP33", "EP34"]
    assert [shot["shot_id"] for shot in result["shots"]] == ["S33-1", "S34-1"]
    stored = list((tmp_path / "jobs" / result["preflight_id"]).glob("EP*.ready/*.txt"))
    assert [path.read_text(encoding="utf-8") for path in sorted(stored)] == split_episode_text(text)


def test_history_and_media_use_recorded_comfy_output(config, tmp_path):
    output_root = tmp_path / "runs"
    comfy_output = tmp_path / "comfy-output"
    output_root.joinpath("run-1").mkdir(parents=True)
    comfy_output.mkdir()
    comfy_output.joinpath("shot.mp4").write_bytes(b"video")
    output_root.joinpath("run-1", "run.json").write_text(json.dumps({
        "batch_id": "run-1", "episode": "EP33", "started_at": "2026-10-04T00:00:00Z",
        "finished_at": "2026-10-04T00:01:00Z", "summary": {"success": 1, "failed": 0},
        "shots": [{"shot_id": "S33-1", "status": "SUCCESS", "output": {
            "filename": "shot.mp4", "subfolder": "", "type": "output",
        }}],
    }), encoding="utf-8")
    controller = UiController(
        replace(config, output_root=output_root, comfy_output=comfy_output),
        workspace=tmp_path / "jobs",
    )
    assert controller.history()[0]["video_count"] == 1
    assert controller.media_path("run-1", 0) == comfy_output / "shot.mp4"
    with pytest.raises(ConfigurationError):
        controller.run_record("../escape")


def test_ui_state_advertises_codex_asset_bundle_support(config, tmp_path):
    state = UiController(config, workspace=tmp_path / "jobs").state()
    assert state["capabilities"]["codex_asset_bundles"] is True


def test_ui_preflight_uses_isolated_codex_asset_bundle(config, tmp_path, monkeypatch):
    class SchemaClient:
        def __init__(self, base_url):
            pass

        def get_h3_reference_limit(self):
            return 9

    monkeypatch.setattr(ui_module, "RealComfyClient", SchemaClient)
    workspace = tmp_path / "ui_jobs"
    controller = UiController(config, workspace=workspace)
    bundle_id = "a" * 32
    bundle = tmp_path / "codex_uploads" / bundle_id
    (bundle / "assets").mkdir(parents=True)
    asset = bundle / "assets" / "R01.png"
    asset.write_bytes(b"image")
    (bundle / "asset_manifest.json").write_text(
        json.dumps({"R01": "assets/R01.png"}), encoding="utf-8",
    )
    prompt = (
        "SHOT 33-1 TEST\nSHOT DURATION: 5.2 seconds\n"
        "<Picture 1> = Asset R01.\n\n[PROMPT]\nTest.\n"
    )
    result = controller.preflight([{"name": "shot.txt", "text": prompt}], bundle_id)
    assert result["ready"] is True
    assert result["shots"][0]["mappings"][0]["path"] == str(asset)


@pytest.mark.parametrize("bundle_id", ["../escape", "A" * 32, "short"])
def test_ui_preflight_rejects_invalid_codex_bundle_id(config, tmp_path, bundle_id):
    controller = UiController(config, workspace=tmp_path / "ui_jobs")
    with pytest.raises(ConfigurationError, match="Invalid asset bundle ID"):
        controller.preflight([{"name": "shot.txt", "text": "x"}], bundle_id)
