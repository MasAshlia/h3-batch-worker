import json
import subprocess
import sys

import pytest
import yaml

from h3_worker.cli import main


def write_config(project_root, tmp_path):
    data = yaml.safe_load((project_root / "config.example.yaml").read_text(encoding="utf-8"))
    data["workflow"]["template"] = str(project_root / data["workflow"]["template"])
    data["paths"]["asset_manifest"] = str(project_root / data["paths"]["asset_manifest"])
    data["paths"]["output_root"] = str(tmp_path / "runs")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_dry_run_command_succeeds_without_network(project_root, tmp_path):
    result = subprocess.run([sys.executable, "-m", "h3_worker.cli", "dry-run",
                             str(project_root / "fixtures/manifests/episode_test.json"),
                             "--config", str(write_config(project_root, tmp_path))],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "20 valid" in result.stdout
    assert "0 invalid" in result.stdout
    assert "READY FOR SUBMISSION" in result.stdout
    assert "TEST FIXTURE ONLY" in result.stdout
    assert str(project_root) not in result.stdout
    assert not (tmp_path / "runs").exists()


def test_dry_run_failure_exit_code_and_structured_error(project_root, tmp_path):
    (tmp_path / "bad.txt").write_text("duration: 5\n@image_1: 雨夜街道", encoding="utf-8")
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps({"episode": "EP_BAD", "shots": [
        {"shot_id": "S004", "prompt_file": "bad.txt"},
    ]}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "h3_worker.cli", "dry-run", str(manifest),
                             "--config", str(write_config(project_root, tmp_path))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "MissingAssetError" in result.stdout
    assert "雨夜街道" in result.stdout
    assert "BATCH NOT READY" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_mock_run_command_generates_twenty_shot_run_json(project_root, tmp_path):
    result = subprocess.run([sys.executable, "-m", "h3_worker.cli", "run",
                             str(project_root / "fixtures/manifests/episode_test.json"), "--mock",
                             "--config", str(write_config(project_root, tmp_path))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "Success: 20\nFailed: 0" in result.stdout
    files = list((tmp_path / "runs").glob("*/run.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(record["shots"]) == 20
    assert record["summary"] == {"success": 20, "failed": 0}
    assert all(shot["status"] == "SUCCESS" for shot in record["shots"])
    assert str(project_root) not in result.stderr


def test_mock_failure_options_and_nonzero_exit(project_root, tmp_path, capsys):
    (tmp_path / "one.txt").write_text("duration: 5\n@image_1: 林默", encoding="utf-8")
    manifest = tmp_path / "episode.json"
    manifest.write_text(json.dumps({"episode": "TEST", "shots": [
        {"shot_id": "S003", "prompt_file": "one.txt"},
    ]}), encoding="utf-8")
    code = main(["run", str(manifest), "--mock", "--mock-fail", "S003",
                 "--config", str(write_config(project_root, tmp_path))])
    assert code == 1
    assert "Success: 0\nFailed: 1" in capsys.readouterr().out


def test_bad_batch_mock_run_writes_no_run(project_root, tmp_path):
    manifest = tmp_path / "episode.json"
    manifest.write_text(json.dumps({"episode": "TEST", "shots": [
        {"shot_id": "S001", "prompt_file": "missing.txt"},
    ]}), encoding="utf-8")
    assert main(["run", str(manifest), "--mock", "--config", str(write_config(project_root, tmp_path))]) == 1
    assert not (tmp_path / "runs").exists()


def test_real_run_is_not_open_in_v01():
    with pytest.raises(SystemExit) as captured:
        main(["run", "episode.json"])
    assert captured.value.code == 2
