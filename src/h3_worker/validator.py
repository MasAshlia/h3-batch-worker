import re

from .errors import InvalidPromptFormatError
from .models import ShotTask


def validate_task(task: ShotTask) -> None:
    if not isinstance(task.shot_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task.shot_id):
        raise InvalidPromptFormatError("shot_id must use letters, digits, underscores or hyphens")
    if not isinstance(task.prompt, str) or not task.prompt.strip():
        raise InvalidPromptFormatError("Prompt must be non-empty text")
    if type(task.duration_seconds) is not int or task.duration_seconds <= 0:
        raise InvalidPromptFormatError("Duration must be a positive integer")
    if type(task.steps) is not int or task.steps != task.duration_seconds * 3:
        raise InvalidPromptFormatError("Steps must equal duration_seconds × 3")
    if not isinstance(task.references, dict) or not 1 <= len(task.references) <= 9:
        raise InvalidPromptFormatError("One to nine references are required")
    for key, name in task.references.items():
        if (not isinstance(key, str) or not re.fullmatch(r"image_[1-9]", key)
                or not isinstance(name, str) or not name.strip()):
            raise InvalidPromptFormatError("Invalid reference key or asset name")
    if task.seed is not None and (type(task.seed) is not int or task.seed < 0):
        raise InvalidPromptFormatError("Seed must be a non-negative integer")
