import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def runner(project_root, tmp_path, monkeypatch):
    path = project_root / "scripts/windows/test_project.py"
    spec = importlib.util.spec_from_file_location("windows_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "中文 project"
    root.mkdir()
    monkeypatch.setattr(module, "__file__", str(root / "scripts/windows/test_project.py"))
    return module, root


def complete_record(root, name="new"):
    path = root / "runs" / name / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "summary": {"success": 20, "failed": 0}, "finished_at": "now",
        "shots": [{"status": "SUCCESS", "prompt_id": str(i), "finished_at": "now"} for i in range(20)],
    }), encoding="utf-8")
    (path.parent / "run.log").write_text("status log", encoding="utf-8")
    return path


def test_runner_creates_venv_uses_fixture_config_and_checks_new_record(runner, monkeypatch):
    module, root = runner
    complete_record(root, "old")
    calls = []

    def command(arguments, *, cwd, env, check):
        assert cwd == root and check is True
        assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
        calls.append(arguments)
        if "--mock" in arguments:
            complete_record(root)

    monkeypatch.setattr(module.subprocess, "run", command)
    assert module.main() == 0
    assert calls[0][1:3] == ["-m", "venv"]
    assert calls[1][1:] == ["-m", "pip", "install", "-e", ".[test]"]
    assert calls[3][1:] == ["-m", "pytest"]
    assert calls[4][-2:] == ["--config", "config.example.yaml"]
    assert calls[5][-2:] == ["--config", "config.example.yaml"]
    assert "--mock" in calls[5]


@pytest.mark.parametrize("failure", ["pip", "pytest", "dry-run", "--mock"])
def test_runner_stops_after_failing_stage(runner, monkeypatch, failure):
    module, _ = runner
    calls = []

    def command(arguments, **kwargs):
        calls.append(arguments)
        if failure in arguments:
            raise subprocess.CalledProcessError(2, arguments)

    monkeypatch.setattr(module.subprocess, "run", command)
    assert module.main() == 1
    assert failure in calls[-1]


def test_runner_rejects_stale_run_when_command_writes_nothing(runner, monkeypatch):
    module, root = runner
    complete_record(root, "old")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    assert module.main() == 1


@pytest.mark.parametrize("missing", ["finished_at", "shots", "summary"])
def test_runner_rejects_incomplete_record(runner, missing):
    module, root = runner
    path = complete_record(root)
    record = json.loads(path.read_text(encoding="utf-8"))
    del record[missing]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        module.verify_run(path)
