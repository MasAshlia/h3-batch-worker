from copy import deepcopy
import re

from .errors import WorkflowError
from .models import ShotTask
from .validator import validate_task


class WorkflowPatcher:
    def __init__(self, template: dict, mapping: dict):
        if not isinstance(template, dict) or not template:
            raise WorkflowError("Workflow must be a non-empty API-format node object")
        self.template = deepcopy(template)
        self.mapping = deepcopy(mapping)
        allowed_keys = {
            "prompt_node", "reference_node", "duration_node", "scheduler_node",
            "prompt_input", "duration_input", "steps_input", "reference_inputs",
            "seed_node", "seed_input",
        }
        if set(mapping) - allowed_keys or ("seed_input" in mapping and "seed_node" not in mapping):
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
        else:
            if "reference_node" not in mapping:
                raise WorkflowError("Missing reference_node or reference_inputs mapping")
            self.reference_targets = {
                f"ref_image_{i}": (mapping["reference_node"], f"ref_image_{i}") for i in range(9)
            }
        targets.extend(self.reference_targets.values())
        self.seed_target = None
        if "seed_node" in mapping:
            self.seed_target = (mapping["seed_node"], mapping.get("seed_input", "seed"))
            targets.append(self.seed_target)
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

    def patch(self, task: ShotTask, image_values: dict[str, str]) -> dict:
        validate_task(task)
        if set(image_values) != set(task.reference_slots):
            raise WorkflowError("Resolved image slots must match task references exactly")
        if not set(image_values) <= set(self.reference_targets):
            raise WorkflowError("Workflow mapping does not cover all task reference slots")
        if task.seed is not None and self.seed_target is None:
            raise WorkflowError("Task seed requires seed_node mapping")
        workflow = deepcopy(self.template)

        def write(target, value):
            node_id, input_name = target
            workflow[node_id]["inputs"][input_name] = value

        write(self.prompt_target, task.prompt)
        write(self.duration_target, float(task.duration_seconds))
        write(self.steps_target, task.steps)
        # Clear mapped unused slots so template example assets cannot leak into a shot.
        for slot, target in self.reference_targets.items():
            value = image_values.get(slot, "")
            if not isinstance(value, str) or (slot in image_values and not value):
                raise WorkflowError("Image input values must be non-empty strings")
            write(target, value)
        if task.seed is not None:
            write(self.seed_target, task.seed)
        return workflow

    @property
    def is_test_fixture(self) -> bool:
        return any(
            isinstance(node, dict)
            and isinstance(node.get("_meta"), dict)
            and "TEST FIXTURE ONLY" in str(node.get("_meta", {}).get("title", ""))
            for node in self.template.values()
        )
