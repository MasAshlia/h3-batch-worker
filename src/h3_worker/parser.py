import re

from .errors import (
    InvalidPromptFormatError,
    MissingDurationError,
    MissingReferenceError,
)
from .models import ShotTask

_DURATION = re.compile(r"duration:\s*([1-9][0-9]*)\s*(?:s|秒)?")
_REFERENCE = re.compile(r"@image_([1-9]):\s*(\S(?:.*\S)?)")
_MENTION = re.compile(r"@image_[A-Za-z0-9_]*")


def parse_prompt(shot_id: str, prompt: str, seed: int | None = None) -> ShotTask:
    """Read metadata before the first blank line; preserve the original text."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidPromptFormatError("Prompt must be non-empty text")
    if seed is not None and (type(seed) is not int or seed < 0):
        raise InvalidPromptFormatError("Seed must be a non-negative integer")
    duration = None
    references = {}
    # A leading blank line is ambiguous, so it does not silently skip the header.
    for number, raw in enumerate(prompt.splitlines(), 1):
        line = raw.strip()
        if not line:
            break
        if line.startswith("duration:"):
            match = _DURATION.fullmatch(line)
            if not match or duration is not None:
                raise InvalidPromptFormatError(
                    "Duration must appear once and be a positive integer in seconds"
                )
            duration = int(match[1])
        elif line.startswith("@image_"):
            match = _REFERENCE.fullmatch(line)
            if not match:
                raise InvalidPromptFormatError(
                    "Reference declarations must use @image_1 through @image_9"
                )
            key = f"image_{match[1]}"
            if key in references:
                raise InvalidPromptFormatError(f"Duplicate declaration: @{key}")
            references[key] = match[2]
        else:
            raise InvalidPromptFormatError(f"Unrecognized header field at line {number}")
    if duration is None:
        raise MissingDurationError("Missing duration in Prompt header")
    if not references:
        raise MissingReferenceError("At least one reference declaration is required")
    for token in _MENTION.findall(prompt):
        if not re.fullmatch(r"@image_[1-9]", token):
            raise InvalidPromptFormatError("Only @image_1 through @image_9 are supported")
        if token[1:] not in references:
            raise MissingReferenceError(f"Missing declaration: {token}")
    references = dict(sorted(references.items()))
    return ShotTask(shot_id, prompt, duration, duration * 3, references, seed)
