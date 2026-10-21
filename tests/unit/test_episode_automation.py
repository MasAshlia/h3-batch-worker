from copy import deepcopy
from dataclasses import replace
import json

import pytest

from h3_worker.asset_resolver import AssetResolver
from h3_worker.batch import compile_episode_folder
from h3_worker.errors import AmbiguousAssetError, ConfigurationError, MissingAssetError, WorkflowError
from h3_worker.parser import parse_prompt
from h3_worker.workflow_patcher import WorkflowPatcher


def _prompt(episode, shot, assets, *, duration="5.1", previous=False):
    mapping = []
    picture = 1
    for asset in assets[:1]:
        mapping.append(f"Asset {asset} = <Picture {picture}>")
        picture += 1
    if previous:
        mapping.append(f"<Picture {picture}> = previous shot final frame")
        picture += 1
    for asset in assets[1:]:
        mapping.append(f"Asset {asset} = <Picture {picture}>")
        picture += 1
    return (
        f"SHOT {episode}-{shot} TEST\n"
        f"SHOT DURATION: {duration} seconds\n\n"
        "[UPLOAD MAPPING]\n" + "\n".join(mapping) + "\n\n"
        "[ACTION]\nUse " + " and ".join(f"<Picture {i}>" for i in range(1, picture))
    )


def _dynamic_template():
    return {
        "1": {"class_type": "Primitive", "inputs": {"text": ""}},
        "2": {"class_type": "Primitive", "inputs": {"value": 5.0}},
        "3": {"class_type": "Scheduler", "inputs": {"steps": 15}},
        "4": {"class_type": "LegacyReferences", "inputs": {"ref_image_0": ""}},
        "10": {
            "class_type": "MiniMaxH3ReferenceToVideo",
            "inputs": {
                "ref_images.ref_image_1": ["8", 0],
                "ref_images.ref_image_2": ["9", 0],
                "ref_videos.ref_video_0": ["8", 0],
                "ref_video_audios.ref_video_audio_0": ["8", 0],
                "ref_audios.ref_audio_0": ["8", 0],
                "keep": 123,
            },
        },
        "8": {"class_type": "LoadImage", "inputs": {"image": "old-one.png"}},
        "9": {"class_type": "LoadImage", "inputs": {"image": "old-two.png"}},
    }


def _dynamic_mapping():
    return {
        "prompt_node": "1", "prompt_input": "text",
        "duration_node": "2", "duration_input": "value",
        "scheduler_node": "3", "steps_input": "steps",
        "reference_node": "4",
    }


def test_unique_scan_cache_and_changed_file_invalidation(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"MANUAL":"manual.png"}', encoding="utf-8")
    (tmp_path / "manual.png").write_bytes(b"manual")
    auto = tmp_path / "R06_Alex_v01.png"
    auto.write_bytes(b"first")
    index = tmp_path / "generated.json"

    resolver = AssetResolver(manifest, asset_roots=(tmp_path,), index_path=index)
    paths, audit = resolver.resolve_detailed({"ref_image_0": "R06"})
    assert paths["ref_image_0"] == auto
    assert audit["ref_image_0"]["source"] == "unique_scan"

    resolver = AssetResolver(manifest, asset_roots=(tmp_path,), index_path=index)
    assert resolver.resolve_detailed({"ref_image_0": "R06"})[1]["ref_image_0"]["source"] == "generated_index"
    auto.write_bytes(b"changed")
    resolver = AssetResolver(manifest, asset_roots=(tmp_path,), index_path=index)
    assert resolver.resolve_detailed({"ref_image_0": "R06"})[1]["ref_image_0"]["source"] == "unique_scan"


def test_empty_manifest_allows_unique_asset_scan(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    asset = tmp_path / "R01-V01_Kane.png"
    asset.write_bytes(b"image")

    resolved = AssetResolver(manifest, asset_roots=(tmp_path,)).resolve({"ref_image_0": "R01-V01"})

    assert resolved["ref_image_0"] == asset


def test_scan_rejects_zero_and_ambiguous_candidates(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"MANUAL":"manual.png"}', encoding="utf-8")
    (tmp_path / "manual.png").write_bytes(b"manual")
    resolver = AssetResolver(manifest, asset_roots=(tmp_path,))
    with pytest.raises(MissingAssetError) as missing:
        resolver.resolve({"ref_image_0": "R99"})
    assert missing.value.details["candidates"] == []
    (tmp_path / "R06_one.png").write_bytes(b"1")
    (tmp_path / "R06-two.jpg").write_bytes(b"2")
    with pytest.raises(AmbiguousAssetError) as ambiguous:
        resolver.resolve({"ref_image_0": "R06"})
    assert len(ambiguous.value.details["candidates"]) == 2


@pytest.mark.parametrize("count", [1, 4, 9])
def test_dynamic_reference_nodes_are_created_without_mutating_template(count):
    template = _dynamic_template()
    original = deepcopy(template)
    patcher = WorkflowPatcher(template, _dynamic_mapping())
    references = {f"image_{i}": f"R{i:02d}" for i in range(1, count + 1)}
    task = parse_prompt("S1", "body", duration_seconds=5, references=references)
    images = {f"ref_image_{i - 1}": f"upload/{i}.png" for i in range(1, count + 1)}
    workflow, mapping = patcher.patch_with_mapping(task, images)

    h3_inputs = workflow["10"]["inputs"]
    assert h3_inputs["keep"] == 123
    assert sorted(key for key in h3_inputs if key.startswith("ref_images.")) == [
        f"ref_images.ref_image_{i}" for i in range(1, count + 1)
    ]
    assert not any(key.startswith("ref_videos.") for key in h3_inputs)
    assert not any(key.startswith("ref_video_audios.") for key in h3_inputs)
    assert not any(key.startswith("ref_audios.") for key in h3_inputs)
    assert [mapping[f"ref_image_{i - 1}"]["load_node"] for i in range(1, count + 1)] == [
        str(i) for i in range(11, 11 + count)
    ]
    assert patcher.template == original
    assert template == original


def test_dynamic_reference_limit_follows_server_schema():
    patcher = WorkflowPatcher(_dynamic_template(), _dynamic_mapping(), max_reference_images=3)
    references = {f"image_{i}": f"R{i}" for i in range(1, 5)}
    task = parse_prompt("S1", "body", duration_seconds=5, references=references)
    with pytest.raises(WorkflowError, match="at most 3"):
        patcher.patch(task, {f"ref_image_{i}": f"{i}.png" for i in range(4)})


def test_episode_folder_sorts_shots_and_records_prompt_transformations(config, tmp_path):
    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps({
        "R01": str(config.asset_manifest.parent / "linmo.png"),
        "R02": str(config.asset_manifest.parent / "suqing.png"),
    }), encoding="utf-8")
    folder = tmp_path / "EP33.ready"
    folder.mkdir()
    (folder / "later.txt").write_text(_prompt(33, 16, ["R01", "R02"], previous=True), encoding="utf-8")
    (folder / "earlier.txt").write_text(_prompt(33, 2, ["R01"]), encoding="utf-8")

    report, _ = compile_episode_folder(
        folder,
        replace(config, asset_manifest=assets, asset_roots=(tmp_path,), asset_index=tmp_path / "index.json"),
    )
    assert report.ready
    assert [item.shot_id for item in report.items] == ["S33-2", "S33-16"]
    task = report.items[1].compiled.task
    assert task.raw_duration_seconds == 5.1
    assert task.duration_seconds == 6
    assert task.steps == 18
    assert task.references == {"image_1": "R01", "image_2": "R02"}
    assert "removed_previous_shot_final_frame" in task.transformations
    assert "renumbered_static_pictures" in task.transformations
    assert "<Picture 3>" not in task.prompt


def test_episode_folder_rejects_mixed_episode_before_execution(config, tmp_path):
    folder = tmp_path / "EP33.ready"
    folder.mkdir()
    (folder / "wrong.txt").write_text(_prompt(34, 1, ["R01"]), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="does not match"):
        compile_episode_folder(folder, config)

@pytest.mark.parametrize("continuity_value, expected", [("tail.png", ["8", 0]), (None, None)])
def test_dynamic_rebuild_handles_continuity_ref_link(continuity_value, expected):
    template = _dynamic_template()
    template["10"]["inputs"]["ref_images.ref_image_3"] = ["8", 0]
    patcher = WorkflowPatcher(
        template,
        {**_dynamic_mapping(), "continuity_input": {"node": "8", "input": "image"}},
    )
    task = parse_prompt("S1", "body", duration_seconds=5, references={"image_1": "R1"})

    workflow = patcher.patch(
        task, {"ref_image_0": "upload/one.png"}, continuity_value=continuity_value,
    )

    assert workflow["10"]["inputs"].get("ref_images.ref_image_3") == expected
