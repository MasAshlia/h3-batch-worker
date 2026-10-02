from copy import deepcopy

import pytest

from h3_worker.errors import WorkflowError
from h3_worker.parser import parse_prompt
from h3_worker.workflow_patcher import WorkflowPatcher


def test_patch_preserves_prompt_and_unrelated_fields_and_template(patcher, template):
    original = deepcopy(template)
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默\n\n完整正文")
    result = patcher.patch(task, {"ref_image_0": "linmo.png"})
    assert result["100"]["inputs"]["text"] == task.prompt
    assert result["300"]["inputs"]["value"] == 5.0
    assert type(result["300"]["inputs"]["value"]) is float
    assert result["400"]["inputs"]["steps"] == 15
    assert result["400"]["inputs"]["seed"] == 42
    assert result["200"]["inputs"]["ref_image_0"] == "linmo.png"
    assert result["900"] == original["900"]
    assert result["100"]["inputs"]["preserve"] == "unchanged"
    result["900"]["inputs"]["connection"][0] = "CHANGED"
    assert patcher.template == original
    assert template == original


def test_second_task_does_not_inherit_first_references(patcher):
    first = parse_prompt("S001", "duration: 5\n@image_1: 林默\n@image_9: 客厅")
    second = parse_prompt("S002", "duration: 8\n@image_1: 苏晴")
    result1 = patcher.patch(first, {"ref_image_0": "one.png", "ref_image_8": "nine.png"})
    result2 = patcher.patch(second, {"ref_image_0": "two.png"})
    assert result1["200"]["inputs"]["ref_image_8"] == "nine.png"
    assert result2["200"]["inputs"]["ref_image_8"] == ""
    assert result2["400"]["inputs"]["steps"] == 24


def test_node_ids_and_input_names_configurable(template):
    template["501"] = template.pop("100")
    template["501"]["inputs"]["prompt"] = template["501"]["inputs"].pop("text")
    mapping = {"prompt_node": "501", "prompt_input": "prompt", "reference_node": "200",
               "duration_node": "300", "scheduler_node": "400"}
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默")
    result = WorkflowPatcher(template, mapping).patch(task, {"ref_image_0": "one.png"})
    assert result["501"]["inputs"]["prompt"] == task.prompt


def test_explicit_load_image_target_preserves_reference_links(template, config):
    template["501"] = {"class_type": "TestLoadImage", "inputs": {"image": ""}}
    template["200"]["inputs"]["ref_image_0"] = ["501", 0]
    mapping = {**config.mapping, "reference_inputs": {"ref_image_0": {"node": "501", "input": "image"}}}
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默")
    result = WorkflowPatcher(template, mapping).patch(task, {"ref_image_0": "uploaded/one.png"})
    assert result["501"]["inputs"]["image"] == "uploaded/one.png"
    assert result["200"]["inputs"]["ref_image_0"] == ["501", 0]


def test_seed_requires_explicit_mapping(patcher, template, config):
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默", seed=123)
    with pytest.raises(WorkflowError):
        patcher.patch(task, {"ref_image_0": "one.png"})
    seeded = WorkflowPatcher(template, {**config.mapping, "seed_node": "400"})
    assert seeded.patch(task, {"ref_image_0": "one.png"})["400"]["inputs"]["seed"] == 123


@pytest.mark.parametrize("mapping_change", [
    {"prompt_node": "missing"}, {"prompt_node": 100}, {"prompt_input": "missing"},
    {"prompt_node": "300", "prompt_input": "value"},
    {"reference_inputs": {}}, {"reference_inputs": {"ref_image_9": {"node": "200", "input": "ref_image_0"}}},
    {"promt_input": "text"}, {"seed_input": "seed"},
])
def test_bad_mapping_rejected(template, config, mapping_change):
    with pytest.raises(WorkflowError):
        WorkflowPatcher(template, {**config.mapping, **mapping_change})


def test_mapping_must_not_overwrite_node_connection(template, config):
    template["200"]["inputs"]["ref_image_0"] = ["501", 0]
    with pytest.raises(WorkflowError, match="node link"):
        WorkflowPatcher(template, config.mapping)


def test_missing_resolved_reference_rejected(patcher):
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默")
    with pytest.raises(WorkflowError):
        patcher.patch(task, {})


def test_safe_output_prefix_uses_shot_id(template, config):
    template["92"] = {"class_type": "SaveVideo", "inputs": {"filename_prefix": "old"}}
    mapping = {
        **config.mapping,
        "output_node": "92",
        "output_prefix": "REAL_TEST",
    }
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默")
    result = WorkflowPatcher(template, mapping).patch(task, {"ref_image_0": "one.png"})
    assert result["92"]["inputs"]["filename_prefix"] == "REAL_TEST_S001"
