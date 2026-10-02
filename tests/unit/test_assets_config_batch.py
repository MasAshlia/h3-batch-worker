from dataclasses import replace
import json

import pytest

from h3_worker.asset_resolver import AssetResolver
from h3_worker.batch import compile_batch
from h3_worker.config import load_config
from h3_worker.errors import ConfigurationError, DuplicateAssetError, MissingAssetError


def test_asset_paths_are_relative_to_asset_manifest(config):
    resolver = AssetResolver(config.asset_manifest)
    paths = resolver.resolve({"ref_image_0": "林默"})
    assert paths["ref_image_0"] == config.asset_manifest.parent / "linmo.png"


def test_missing_asset_name(config):
    with pytest.raises(MissingAssetError, match="雨夜街道"):
        AssetResolver(config.asset_manifest).resolve({"ref_image_0": "雨夜街道"})


def test_missing_asset_file(tmp_path):
    manifest = tmp_path / "assets.json"
    manifest.write_text('{"林默": "missing.png"}', encoding="utf-8")
    resolver = AssetResolver(manifest)
    with pytest.raises(MissingAssetError):
        resolver.resolve({"ref_image_0": "林默"})


def test_duplicate_asset_name_rejected_before_json_can_overwrite(tmp_path):
    manifest = tmp_path / "assets.json"
    manifest.write_text('{"林默":"one.png","林默":"two.png"}', encoding="utf-8")
    with pytest.raises(DuplicateAssetError):
        AssetResolver(manifest)


def test_missing_unused_asset_fails_batch(config, project_root, tmp_path):
    manifest = tmp_path / "assets.json"
    data = {"林默": str(config.asset_manifest.parent / "linmo.png"), "unused": "missing.png"}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MissingAssetError):
        compile_batch(project_root / "fixtures/manifests/episode_test.json", replace(config, asset_manifest=manifest))


def test_twenty_task_batch(batch):
    report, _ = batch
    assert report.episode == "EP_TEST"
    assert report.ready
    assert len(report.tasks) == 20
    assert report.tasks[0].task.duration_seconds == 5
    assert report.tasks[1].task.steps == 24


def test_batch_accepts_explicit_metadata_for_production_prompt(config, tmp_path):
    prompt = tmp_path / "real.txt"
    prompt.write_text("无元数据的真实生产 Prompt", encoding="utf-8")
    manifest = tmp_path / "episode.json"
    manifest.write_text(json.dumps({"episode": "REAL_TEST", "shots": [{
        "shot_id": "S001", "prompt_file": "real.txt", "duration": 5,
        "references": {"image_1": "林默"},
    }]}), encoding="utf-8")
    report, _ = compile_batch(manifest, config)
    assert report.ready
    assert report.tasks[0].task.steps == 15


def test_collects_all_invalid_shots_and_does_not_guess(config, tmp_path):
    (tmp_path / "one.txt").write_text("@image_1: 林默", encoding="utf-8")
    (tmp_path / "two.txt").write_text("duration: 5\n@image_1: 雨夜街道", encoding="utf-8")
    manifest = tmp_path / "episode.json"
    manifest.write_text(json.dumps({"episode": "TEST", "shots": [
        {"shot_id": "S001", "prompt_file": "one.txt"},
        {"shot_id": "S002", "prompt_file": "two.txt"},
    ]}), encoding="utf-8")
    report, _ = compile_batch(manifest, config)
    assert not report.ready
    assert report.tasks == []
    assert [item.errors[0]["type"] for item in report.items] == ["MissingDurationError", "MissingAssetError"]


@pytest.mark.parametrize("shots", [[], [{"shot_id": "S1"}, {"shot_id": "S1"}], [{"shot_id": "../bad"}]])
def test_bad_episode_manifest(config, tmp_path, shots):
    manifest = tmp_path / "episode.json"
    manifest.write_text(json.dumps({"episode": "EP_TEST", "shots": shots}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        compile_batch(manifest, config)


@pytest.mark.parametrize("content", ["[", "[]", "null", "shots: []", '{"episode":"ONE","episode":"TWO"}'])
def test_bad_json(config, tmp_path, content):
    path = tmp_path / "episode.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        compile_batch(path, config)


@pytest.mark.parametrize("content", ["[]", "null", "bad: [", "comfyui: {}\ncomfyui: {}", "? [a, b]\n: value"])
def test_bad_yaml(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_config_relative_paths_ignore_cwd(project_root, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = load_config(project_root / "config.example.yaml")
    assert config.template_path.is_file()
    report, _ = compile_batch(project_root / "fixtures/manifests/episode_test.json", config)
    assert report.ready
