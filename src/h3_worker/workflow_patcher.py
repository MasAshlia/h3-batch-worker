from copy import deepcopy
import re

from .errors import WorkflowError
from .models import ShotTask
from .validator import validate_task


class WorkflowPatcher:
    def __init__(self, template: dict, mapping: dict, *, max_reference_images: int = 9):
        if not isinstance(template, dict) or not template:
            raise WorkflowError("Workflow must be a non-empty API-format node object")
        if type(max_reference_images) is not int or not 0 <= max_reference_images <= 9:
            raise WorkflowError("Reference image limit must be between zero and nine")
        self.template = deepcopy(template)
        self.mapping = deepcopy(mapping)
        self.max_reference_images = max_reference_images
        h3_nodes = [
            node_id for node_id, node in self.template.items()
            if isinstance(node, dict) and node.get("class_type") == "MiniMaxH3ReferenceToVideo"
        ]
        self.dynamic_reference_node = h3_nodes[0] if len(h3_nodes) == 1 else None
        allowed_keys = {
            "prompt_node", "reference_node", "duration_node", "scheduler_node",
            "prompt_input", "duration_input", "steps_input", "reference_inputs",
            "seed_node", "seed_input", "output_node", "output_input", "output_prefix",
        }
        if (set(mapping) - allowed_keys
                or ("seed_input" in mapping and "seed_node" not in mapping)
                or (set(mapping) & {"output_input", "output_prefix"} and "output_node" not in mapping)):
            raise WorkflowError("Unknown or incomplete workflow mapping fields")
        for key in ("prompt_node", "duration_node", "scheduler_node"):
            if key not in mapping:
                raise WorkflowError(f"Missing workflow mapping: {key}")
        self.prompt_target = (mapping["prompt_node"], mapping.get("prompt_input", "text"))
        self.duration_target = (mapping["duration_node"], mapping.get("duration_input", "value"))
        self.steps_target = (mapping["scheduler_node"], mapping.get("steps_input", "steps"))
        targets = [self.prompt_target, self.duration_target, self.steps_target]
        explicit = mapping.get("reference_inputs")
        if explicit is not None:
            if not isinstance(explicit, dict) or not explicit:
                raise WorkflowError("reference_inputs must be a non-empty object")
            self.reference_targets = {}
            for slot, target in explicit.items():
                if (not isinstance(slot, str) or not re.fullmatch(r"ref_image_[0-8]", slot)
                        or not isinstance(target, dict) or set(target) != {"node", "input"}):
                    raise WorkflowError("Invalid explicit reference input mapping")
                self.reference_targets[slot] = (target["node"], target["input"])
        elif "reference_node" in mapping:
            self.reference_targets = {
                f"ref_image_{i}": (mapping["reference_node"], f"ref_image_{i}") for i in range(9)
            }
        elif self.dynamic_reference_node is not None:
            self.reference_targets = {}
        else:
            if "reference_node" not in mapping:
                raise WorkflowError("Missing reference_node or reference_inputs mapping")
        if self.dynamic_reference_node is None:
            targets.extend(self.reference_targets.values())
        self.seed_target = None
        if "seed_node" in mapping:
            self.seed_target = (mapping["seed_node"], mapping.get("seed_input", "seed"))
            targets.append(self.seed_target)
        self.output_target = None
        self.output_prefix = None
        if "output_node" in mapping:
            self.output_target = (mapping["output_node"], mapping.get("output_input", "filename_prefix"))
            self.output_prefix = mapping.get("output_prefix")
            if self.output_prefix is not None and (
                not isinstance(self.output_prefix, str) or not self.output_prefix.strip()
            ):
                raise WorkflowError("output_prefix must be a non-empty string")
            targets.append(self.output_target)
        for target in targets:
            self._check_target(target)
        if len(set(targets)) != len(targets):
            raise WorkflowError("Workflow mapping targets must not overlap")

    def _check_target(self, target: tuple) -> None:
        node_id, input_name = target
        if not isinstance(node_id, str) or not isinstance(input_name, str):
            raise WorkflowError("Workflow node ids and input names must be strings")
        node = self.template.get(node_id)
        if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                or not isinstance(node.get("inputs"), dict) or input_name not in node["inputs"]):
            raise WorkflowError("Mapped node or input is missing from API workflow")
        if isinstance(node["inputs"][input_name], (list, dict)):
            raise WorkflowError("Mapping must target a literal input, never an existing node link")

    def patch_with_mapping(
        self, task: ShotTask, image_values: dict[str, str], *, output_label: str | None = None,
    ) -> tuple[dict, dict]:
        validate_task(task)
        if set(image_values) != set(task.reference_slots):
            raise WorkflowError("Resolved image slots must match task references exactly")
        if self.dynamic_reference_node is None and not set(image_values) <= set(self.reference_targets):
            raise WorkflowError("Workflow mapping does not cover all task reference slots")
        if len(image_values) > self.max_reference_images:
            raise WorkflowError(
                f"Workflow supports at most {self.max_reference_images} reference images"
            )
        if task.seed is not None and self.seed_target is None:
            raise WorkflowError("Task seed requires seed_node mapping")
        workflow = deepcopy(self.template)

        def write(target, value):
            node_id, input_name = target
            workflow[node_id]["inputs"][input_name] = value

        write(self.prompt_target, task.prompt)
        write(self.duration_target, float(task.duration_seconds))
        write(self.steps_target, task.steps)
        reference_mapping = {}
        if self.dynamic_reference_node is not None:
            h3_inputs = workflow[self.dynamic_reference_node]["inputs"]
            for input_name in list(h3_inputs):
                if re.fullmatch(
                    r"(?:ref_images\.ref_image_|ref_videos\.ref_video_"
                    r"|ref_video_audios\.ref_video_audio_|ref_audios\.ref_audio_)[0-9]+",
                    input_name,
                ):
                    del h3_inputs[input_name]
            numeric_ids = [int(node_id) for node_id in workflow if str(node_id).isdigit()]
            next_node_id = max(numeric_ids, default=0) + 1
            ordered_slots = sorted(image_values, key=lambda slot: int(slot.removeprefix("ref_image_")))
            for picture_number, slot in enumerate(ordered_slots, 1):
                value = image_values[slot]
                if not isinstance(value, str) or not value:
                    raise WorkflowError("Image input values must be non-empty strings")
                load_node = str(next_node_id)
                next_node_id += 1
                workflow[load_node] = {
                    "inputs": {"image": value},
                    "class_type": "LoadImage",
                    "_meta": {"title": f"AUTO Reference {picture_number}"},
                }
                h3_input = f"ref_images.ref_image_{picture_number}"
                h3_inputs[h3_input] = [load_node, 0]
                reference_mapping[slot] = {
                    "picture_number": picture_number,
                    "load_node": load_node,
                    "load_input": "image",
                    "h3_node": self.dynamic_reference_node,
                    "h3_input": h3_input,
                }
        else:
            # Clear mapped unused slots so template example assets cannot leak into a shot.
            for slot, target in self.reference_targets.items():
                value = image_values.get(slot, "")
                if not isinstance(value, str) or (slot in image_values and not value):
                    raise WorkflowError("Image input values must be non-empty strings")
                write(target, value)
                if slot in image_values:
                    reference_mapping[slot] = {
                        "load_node": target[0], "load_input": target[1],
                    }
        if task.seed is not None:
            write(self.seed_target, task.seed)
        if self.output_target is not None and self.output_prefix is not None:
            write(self.output_target, f"{self.output_prefix}_{output_label or task.shot_id}")
        return workflow, reference_mapping

    def patch(
        self, task: ShotTask, image_values: dict[str, str], *, output_label: str | None = None,
    ) -> dict:
        return self.patch_with_mapping(task, image_values, output_label=output_label)[0]

    @property
    def is_test_fixture(self) -> bool:
        return any(
            isinstance(node, dict)
            and isinstance(node.get("_meta"), dict)
            and "TEST FIXTURE ONLY" in str(node.get("_meta", {}).get("title", ""))
            for node in self.template.values()
        )
