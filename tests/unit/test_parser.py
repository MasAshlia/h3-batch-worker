import pytest

from h3_worker.errors import InvalidPromptFormatError, MissingDurationError, MissingReferenceError
from h3_worker.parser import parse_prompt
from h3_worker.validator import validate_task


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
