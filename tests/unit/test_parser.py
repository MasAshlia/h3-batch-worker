import pytest

from h3_worker.errors import InvalidPromptFormatError, MissingDurationError, MissingReferenceError
from h3_worker.parser import parse_prompt, parse_shot_identity
from h3_worker.validator import validate_task


def test_external_metadata_keeps_real_prompt_unchanged():
    prompt = "镜头一：人物沿雨巷前行。\n\n镜头二：摄影机缓慢推进。"
    task = parse_prompt(
        "S001",
        prompt,
        duration_seconds=7,
        references={"image_1": "reference_1", "image_2": "reference_2"},
    )
    assert task.prompt == prompt
    assert task.duration_seconds == 7
    assert task.steps == 21
    assert task.references == {"image_1": "reference_1", "image_2": "reference_2"}


def test_structured_h3_prompt_rounds_duration_and_skips_previous_tail():
    prompt = """SHOT 33-16  THE APOLOGY
SHOT DURATION: 5.88 seconds (141 frames at 24 fps).

[UPLOAD MAPPING]
Asset R01-V01 = <Picture 1> = <Subject 1> = (S1).
<Picture 2> = previous shot final frame from SHOT 33-15.
Asset R02-V01 = <Picture 3> = <Subject 2> = (S2).
Asset S07 = <Picture 4> = <Subject 3> = (S3).

[SUBJECT DEFINITIONS]
Asset R02-V01: <Picture 3> and <Subject 2>.
Asset S07: <Picture 4> and <Subject 3>.

[PREVIOUS-TAIL-FRAME USE]
Use <Picture 2> only for carried continuity.

[SUMMARY]
The apology.
"""
    task = parse_prompt("S33-16", prompt)
    assert task.duration_seconds == 6
    assert task.steps == 18
    assert task.references == {
        "image_1": "R01-V01", "image_2": "R02-V01", "image_3": "S07",
    }
    assert "previous shot final frame" not in task.prompt
    assert "Asset R02-V01 = <Picture 2>" in task.prompt
    assert "Asset S07 = <Picture 3>" in task.prompt
    assert "No previous-shot final-frame input is provided" in task.prompt


def test_duration_first_prompt_and_picture_first_assets_are_accepted():
    prompt = """SHOT DURATION: 6.58 seconds (158 frames at 24 fps).

SHOT 33-01 - THE ROOFTOP MEETING

[UPLOAD MAPPING]
<Picture 1> = Asset R01-V01.
<Picture 2> = previous shot final frame from SHOT 32-10.
<Picture 3> = Asset S07.

[PROMPT]
Use <Picture 1>, <Picture 2> and <Picture 3>.
"""
    assert parse_shot_identity(prompt) == ("EP33", "S33-1", 1)
    task = parse_prompt("S33-1", prompt)
    assert task.references == {"image_1": "R01-V01", "image_2": "S07"}
    assert task.duration_seconds == 7
    assert "previous shot final frame" not in task.prompt
    assert "<Picture 2> = Asset S07." in task.prompt


def test_parse_shot_identity_accepts_short_episode_headers():
    assert parse_shot_identity("## SHOT E16-01 - START\nSHOT DURATION: 7 seconds") == (
        "EP16", "S16-1", 1,
    )


@pytest.mark.parametrize(
    "raw,rounded,steps", [("5", 5, 15), ("5.01", 6, 18), ("8.99", 9, 27), ("9", 9, 27)],
)
def test_structured_duration_boundaries(raw, rounded, steps):
    task = parse_prompt(
        "S1", f"SHOT 1-1 TEST\nSHOT DURATION: {raw} seconds\n\n"
        "[UPLOAD MAPPING]\nAsset R01 = <Picture 1>\n",
    )
    assert task.raw_duration_seconds == float(raw)
    assert task.duration_seconds == rounded
    assert task.steps == steps


@pytest.mark.parametrize(("raw", "expected"), [("4.99", 5), ("9.01", 9)])
def test_structured_duration_outside_five_to_nine_is_clamped(raw, expected):
    task = parse_prompt(
        "S1", f"SHOT 1-1 TEST\nSHOT DURATION: {raw} seconds\n\n"
        "[UPLOAD MAPPING]\nAsset R01 = <Picture 1>\n",
    )
    assert task.duration_seconds == expected
    assert "clamped_duration_to_h3_range" in task.transformations


def test_preserves_full_prompt_and_steps():
    prompt = "duration: 5\n@image_1: 林默\n@image_2: 客厅\n\n@image_1 进入 @image_2。\n"
    task = parse_prompt("S001", prompt)
    assert task.prompt == prompt
    assert task.duration_seconds == 5
    assert task.steps == 15
    assert task.references == {"image_1": "林默", "image_2": "客厅"}
    validate_task(task)


@pytest.mark.parametrize("value,steps", [("5", 15), ("8", 24), ("5s", 15), ("8 秒", 24)])
def test_valid_durations(value, steps):
    assert parse_prompt("S001", f"duration: {value}\n@image_1: 林默").steps == steps


def test_missing_duration():
    with pytest.raises(MissingDurationError):
        parse_prompt("S001", "@image_1: 林默\n\n正文")


@pytest.mark.parametrize("value", ["0", "-1", "5.5", "five", "5-8", "", "05", "true", "5sec"])
def test_wrong_duration(value):
    with pytest.raises(InvalidPromptFormatError):
        parse_prompt("S001", f"duration: {value}\n@image_1: 林默")


def test_missing_references():
    with pytest.raises(MissingReferenceError):
        parse_prompt("S001", "duration: 5\n\n正文")


@pytest.mark.parametrize("count", [1, 9])
def test_reference_counts(count):
    task = parse_prompt("S001", "duration: 5\n" + "\n".join(
        f"@image_{i}: 资产{i}" for i in range(1, count + 1)))
    assert len(task.references) == count


def test_more_than_nine_references():
    with pytest.raises(InvalidPromptFormatError):
        parse_prompt("S001", "duration: 5\n" + "\n".join(
            f"@image_{i}: 资产{i}" for i in range(1, 11)))


@pytest.mark.parametrize("index,slot", [(1, "ref_image_0"), (9, "ref_image_8")])
def test_off_by_one_and_sparse_reference(index, slot):
    task = parse_prompt("S001", f"duration: 5\n@image_{index}: 资产")
    assert task.reference_slots == {slot: "资产"}


def test_undeclared_body_reference():
    with pytest.raises(MissingReferenceError):
        parse_prompt("S001", "duration: 5\n@image_1: 林默\n\n@image_2 走进房间")


@pytest.mark.parametrize("header", [
    "duration: 5\nduration: 8\n@image_1: 林默",
    "duration: 5\n@image_1: 林默\n@image_1: 苏晴",
    "duration: 5\n@image_0: 林默",
    "duration: 5\n@image_01: 林默",
    "duration: 5\n@image_1:",
    "镜头描述\nduration: 5\n@image_1: 林默",
    "duration: 5\n@image_1: 林默\n正文没有空行分隔",
])
def test_ambiguous_formats_fail(header):
    with pytest.raises(InvalidPromptFormatError):
        parse_prompt("S001", header)


@pytest.mark.parametrize("token", ["@image_10", "@image_0", "@image_x", "@image_1abc"])
def test_invalid_body_tokens(token):
    with pytest.raises(InvalidPromptFormatError):
        parse_prompt("S001", f"duration: 5\n@image_1: 林默\n\n{token}")


def test_structured_error():
    error = MissingDurationError("Missing duration")
    assert error.to_dict() == {"type": "MissingDurationError", "message": "Missing duration"}


@pytest.mark.parametrize("seed", [-1, True, "12"])
def test_invalid_seed(seed):
    with pytest.raises(InvalidPromptFormatError):
        parse_prompt("S001", "duration: 5\n@image_1: 林默", seed)


def test_validator_rejects_inconsistent_steps():
    task = parse_prompt("S001", "duration: 5\n@image_1: 林默", seed=0)
    task.steps = 14
    with pytest.raises(InvalidPromptFormatError):
        validate_task(task)
