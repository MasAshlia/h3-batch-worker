import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


@pytest.fixture
def bridge(project_root):
    path = project_root / "codex-skills" / "h3-video-production" / "scripts" / "h3_codex.py"
    spec = importlib.util.spec_from_file_location("h3_codex_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_bundle_preserves_prompt_and_normalizes_packaged_manifest(bridge, tmp_path):
    project = tmp_path / "project"
    (project / "private").mkdir(parents=True)
    prompt = tmp_path / "shot.txt"
    original = b"SHOT 15-1 TEST\nSHOT DURATION: 5.2 seconds\n\n<Picture 1> = Asset R01.\n"
    prompt.write_bytes(original)
    package = tmp_path / "assets.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("nested/R01-face.png", b"image")
        archive.writestr("asset_manifest.json", json.dumps({"R01": "nested/R01-face.png"}))

    bundle_id, files, bundle = bridge.stage_bundle(project, [prompt], [package])

    assert len(bundle_id) == 32
    assert (bundle / "prompts" / "shot.txt").read_bytes() == original
    assert files[0]["text"].startswith("SHOT 15-1")
    assert json.loads((bundle / "asset_manifest.json").read_text()) == {
        "R01": "assets/nested/R01-face.png",
    }


def test_stage_bundle_rejects_zip_path_traversal(bridge, tmp_path):
    project = tmp_path / "project"
    (project / "private").mkdir(parents=True)
    prompt = tmp_path / "shot.txt"
    prompt.write_text("prompt", encoding="utf-8")
    package = tmp_path / "assets.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../R01.png", b"image")
    with pytest.raises(bridge.BridgeError, match="unsafe ZIP path"):
        bridge.stage_bundle(project, [prompt], [package])


def test_shot_counts_apply_default_and_overrides(bridge):
    preflight = {"shots": [
        {"shot_id": "S15-1", "ready": True},
        {"shot_id": "S15-2", "ready": True},
    ]}
    assert bridge._shot_counts(preflight, 2, ["S15-2=4"]) == {"S15-1": 2, "S15-2": 4}
    with pytest.raises(bridge.BridgeError):
        bridge._shot_counts(preflight, 1, ["S15-9=2"])


def test_submit_preflights_bundle_then_starts_generation(bridge, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "private").mkdir(parents=True)
    prompt = tmp_path / "shot.txt"
    prompt.write_text("prompt", encoding="utf-8")
    asset = tmp_path / "R01.png"
    asset.write_bytes(b"image")
    calls = []

    monkeypatch.setattr(bridge, "find_project", lambda value: project)
    monkeypatch.setattr(bridge, "ensure_ui", lambda *args: None)

    def request(url, method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/preflight":
            return {"ready": True, "preflight_id": "pf", "shots": [
                {"shot_id": "S15-1", "ready": True},
            ]}
        return {"started": True, "session_id": "pf"}

    monkeypatch.setattr(bridge, "_request", request)
    args = bridge.build_parser().parse_args([
        "submit", "--prompt", str(prompt), "--asset", str(asset),
        "--run-name", "A4 E15 HMS", "--count", "2",
    ])
    result = bridge.submit(args)

    assert result["submitted"] is True
    assert calls[0][1] == "/api/preflight"
    assert len(calls[0][2]["asset_bundle_id"]) == 32
    assert calls[1] == ("POST", "/api/runs", {
        "preflight_id": "pf",
        "generation_counts": {"S15-1": 2},
        "run_name": "A4 E15 HMS",
    })


def test_submit_does_not_start_when_preflight_fails(bridge, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "private").mkdir(parents=True)
    prompt = tmp_path / "shot.txt"
    prompt.write_text("prompt", encoding="utf-8")
    calls = []
    monkeypatch.setattr(bridge, "find_project", lambda value: project)
    monkeypatch.setattr(bridge, "ensure_ui", lambda *args: None)
    monkeypatch.setattr(bridge, "_request", lambda url, method, path, body=None: (
        calls.append(path) or {"ready": False, "preflight_id": "pf", "shots": []}
    ))
    args = bridge.build_parser().parse_args(["submit", "--prompt", str(prompt)])
    with pytest.raises(bridge.BridgeError, match="no generation"):
        bridge.submit(args)
    assert calls == ["/api/preflight"]


def test_ensure_ui_rejects_outdated_running_service(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_request", lambda *args: {"running": False})
    with pytest.raises(bridge.BridgeError, match="outdated"):
        bridge.ensure_ui(tmp_path, "http://127.0.0.1:8765")
