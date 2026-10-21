import re
import math

from .errors import (
    InvalidPromptFormatError,
    MissingDurationError,
    MissingReferenceError,
)
from .models import ShotTask

_DURATION = re.compile(r"duration:\s*([1-9][0-9]*)\s*(?:s|秒)?")
_REFERENCE = re.compile(r"@image_([1-9]):\s*(\S(?:.*\S)?)")
_MENTION = re.compile(r"@image_[A-Za-z0-9_]*")
_H3_DURATION = re.compile(
    r"^SHOT DURATION:\s*([0-9]+(?:\.[0-9]+)?)\s*seconds?\b", re.IGNORECASE | re.MULTILINE,
)
_UPLOAD_MAPPING = re.compile(
    r"^\[UPLOAD MAPPING\]\s*$\n(.*?)(?=^\[|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_ASSET_PICTURE = re.compile(
    r"^Asset\s+([A-Za-z0-9_-]+)\s*=\s*<Picture\s+([1-9])>.*$", re.IGNORECASE,
)
_PICTURE_ASSET = re.compile(
    r"^<Picture\s+([1-9])>\s*=\s*Asset\s+([A-Za-z0-9_-]+)\s*\..*$",
    re.IGNORECASE,
)
_PREVIOUS_PICTURE = re.compile(
    r"^<Picture\s+([1-9])>\s*=\s*previous shot final frame\b.*$", re.IGNORECASE,
)


def _structured_h3_metadata(prompt: str) -> tuple[float, int, dict[str, str], str, list[str]] | None:
    duration_matches = _H3_DURATION.findall(prompt)
    mapping_match = _UPLOAD_MAPPING.search(prompt)
    if not duration_matches and mapping_match is None:
        return None
    if len(duration_matches) != 1:
        raise MissingDurationError("Structured H3 Prompt requires one SHOT DURATION line")
    mapping_lines = (
        mapping_match.group(1).splitlines()
        if mapping_match is not None
        else [line for line in prompt.splitlines() if _PICTURE_ASSET.fullmatch(line.strip())]
    )
    if not mapping_lines:
        raise MissingReferenceError("Structured H3 Prompt requires an UPLOAD MAPPING section or Picture mappings")

    assets = []
    previous_pictures = set()
    for raw in mapping_lines:
        line = raw.strip()
        if not line:
            continue
        asset_match = _ASSET_PICTURE.fullmatch(line)
        picture_asset_match = _PICTURE_ASSET.fullmatch(line)
        previous_match = _PREVIOUS_PICTURE.fullmatch(line)
        if asset_match:
            assets.append((int(asset_match[2]), asset_match[1]))
        elif picture_asset_match:
            assets.append((int(picture_asset_match[1]), picture_asset_match[2]))
        elif previous_match:
            previous_pictures.add(int(previous_match[1]))
        else:
            raise InvalidPromptFormatError("Unsupported UPLOAD MAPPING line")
    if not assets:
        raise MissingReferenceError("UPLOAD MAPPING contains no static assets")
    picture_numbers = [number for number, _ in assets]
    asset_ids = [asset_id for _, asset_id in assets]
    if len(set(picture_numbers)) != len(picture_numbers) or len(set(asset_ids)) != len(asset_ids):
        raise InvalidPromptFormatError("Duplicate asset or Picture number in UPLOAD MAPPING")
    if set(picture_numbers) & previous_pictures:
        raise InvalidPromptFormatError("Picture number cannot be both an asset and previous frame")

    assets.sort()
    normalized = prompt
    placeholders = {}
    for original, _ in assets:
        placeholder = f"<__H3_STATIC_PICTURE_{original}__>"
        placeholders[placeholder] = original
        normalized = re.sub(
            rf"<Picture\s+{original}>", placeholder, normalized, flags=re.IGNORECASE,
        )
    for previous in previous_pictures:
        normalized = re.sub(
            rf"^.*<Picture\s+{previous}>\s*=\s*previous shot final frame.*(?:\n|$)",
            "", normalized, flags=re.IGNORECASE | re.MULTILINE,
        )
        normalized = re.sub(
            rf"<Picture\s+{previous}>", "the opening state", normalized, flags=re.IGNORECASE,
        )
    if previous_pictures:
        normalized = re.sub(
            r"^\[PREVIOUS-TAIL-FRAME USE\]\s*$\n.*?(?=^\[|\Z)",
            "[PREVIOUS-TAIL-FRAME USE]\n"
            "No previous-shot final-frame input is provided. Establish the opening state "
            "from the listed content assets.\n\n",
            normalized,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
    for new_number, (original, _) in enumerate(assets, 1):
        normalized = normalized.replace(
            f"<__H3_STATIC_PICTURE_{original}__>", f"<Picture {new_number}>",
        )

    raw_duration = float(duration_matches[0])
    if not 5 <= raw_duration <= 9:
        raise InvalidPromptFormatError("SHOT DURATION must be between 5 and 9 seconds")
    duration = math.ceil(raw_duration)
    references = {
        f"image_{number}": asset_id
        for number, (_, asset_id) in enumerate(assets, 1)
    }
    transformations = []
    if previous_pictures:
        transformations.append("removed_previous_shot_final_frame")
    if any(original != new for new, (original, _) in enumerate(assets, 1)):
        transformations.append("renumbered_static_pictures")
    if raw_duration != duration:
        transformations.append("rounded_duration_up")
    return raw_duration, duration, references, normalized, transformations


def parse_prompt(
    shot_id: str,
    prompt: str,
    seed: int | None = None,
    *,
    duration_seconds: int | None = None,
    references: dict[str, str] | None = None,
) -> ShotTask:
    """Read metadata before the first blank line; preserve the original text."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidPromptFormatError("Prompt must be non-empty text")
    if seed is not None and (type(seed) is not int or seed < 0):
        raise InvalidPromptFormatError("Seed must be a non-negative integer")
    if duration_seconds is not None or references is not None:
        if (type(duration_seconds) is not int or duration_seconds <= 0
                or not isinstance(references, dict) or not references):
            raise InvalidPromptFormatError(
                "External Prompt metadata requires a positive duration and references"
            )
        if any(
            not isinstance(key, str) or not re.fullmatch(r"image_[1-9]", key)
            or not isinstance(value, str) or not value.strip()
            for key, value in references.items()
        ):
            raise InvalidPromptFormatError("External references must use image_1 through image_9")
        for token in _MENTION.findall(prompt):
            if not re.fullmatch(r"@image_[1-9]", token):
                raise InvalidPromptFormatError("Only @image_1 through @image_9 are supported")
            if token[1:] not in references:
                raise MissingReferenceError(f"Missing declaration: {token}")
        return ShotTask(
            shot_id,
            prompt,
            duration_seconds,
            duration_seconds * 3,
            dict(sorted(references.items())),
            seed,
            original_prompt=prompt,
            raw_duration_seconds=float(duration_seconds),
        )
    structured = _structured_h3_metadata(prompt)
    if structured is not None:
        raw_duration, duration, references, normalized_prompt, transformations = structured
        return ShotTask(
            shot_id,
            normalized_prompt,
            duration,
            duration * 3,
            references,
            seed,
            original_prompt=prompt,
            raw_duration_seconds=raw_duration,
            transformations=transformations,
        )
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
    return ShotTask(
        shot_id, prompt, duration, duration * 3, references, seed,
        original_prompt=prompt, raw_duration_seconds=float(duration),
    )


_SHOT_HEADER = re.compile(
    r"^\s*(?:#+\s*)?SHOT\s+(?:(?P<episode>[0-9]+)-(?P<number>[0-9]+)(?P<suffix>[A-Z]?)|"
    r"EP(?P<episode_ep>[0-9]+)-S(?P<number_ep>[0-9]+)(?P<suffix_ep>[A-Z]?)|"
    r"E(?P<episode_short>[0-9]+)-(?P<number_short>[0-9]+)(?P<suffix_short>[A-Z]?))\b[^\r\n]*",
    re.IGNORECASE | re.MULTILINE,
)


def parse_shot_identity(prompt: str) -> tuple[str, str, int]:
    if not isinstance(prompt, str):
        raise InvalidPromptFormatError("Prompt must be text")
    matches = list(_SHOT_HEADER.finditer(prompt))
    start_matches = [match for match in matches if re.search(r"\bSTART\b", match.group(0), re.IGNORECASE)]
    if start_matches:
        matches = start_matches
    if len(matches) != 1:
        raise InvalidPromptFormatError("Prompt must contain exactly one SHOT identity line")
    match = matches[0]
    episode_number = int(
        match.group("episode") or match.group("episode_ep") or match.group("episode_short")
    )
    shot_number = int(
        match.group("number") or match.group("number_ep") or match.group("number_short")
    )
    suffix = (
        match.group("suffix") or match.group("suffix_ep") or match.group("suffix_short") or ""
    ).upper()
    return f"EP{episode_number}", f"S{episode_number}-{shot_number}{suffix}", shot_number
