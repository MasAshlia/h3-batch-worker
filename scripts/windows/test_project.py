"""One-click logic verification, without touching a ComfyUI Python environment."""

import json
import os
from pathlib import Path
import subprocess
import sys


def verify_run(path: Path) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    if (record.get("summary") != {"success": 20, "failed": 0}
            or len(record.get("shots", [])) != 20
            or not record.get("finished_at")
            or any(shot.get("status") != "SUCCESS" or not shot.get("prompt_id")
                   or not shot.get("finished_at") for shot in record["shots"])
            or not (path.parent / "run.log").is_file()):
        raise ValueError("Mock run record is incomplete or has unexpected results")


def main() -> int:
    if sys.version_info < (3, 12):
        print("Python 3.12+ is required.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[2]
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    def run(arguments):
        subprocess.run(arguments, cwd=root, env=env, check=True)

    try:
        if not python.is_file():
            print("Creating project virtual environment...", flush=True)
            run([sys.executable, "-m", "venv", str(root / ".venv")])
        print("Installing project and test dependencies...", flush=True)
        run([str(python), "-m", "pip", "install", "-e", ".[test]"])
        run([str(python), "-c", "import sys; assert sys.version_info >= (3, 12)"])
        print("[1/3] pytest", flush=True)
        run([str(python), "-m", "pytest"])
        print("[2/3] dry-run", flush=True)
        manifest = "fixtures/manifests/episode_test.json"
        # Explicit example config keeps the smoke test independent of studio config.yaml.
        config = ["--config", "config.example.yaml"]
        run([str(python), "-m", "h3_worker.cli", "dry-run", manifest, *config])
        before = set((root / "runs").glob("*/run.json"))
        print("[3/3] mock batch", flush=True)
        run([str(python), "-m", "h3_worker.cli", "run", manifest, "--mock", *config])
        created = set((root / "runs").glob("*/run.json")) - before
        if len(created) != 1:
            raise ValueError("Expected exactly one new mock run record")
        verify_run(created.pop())
        print("H3 TEST PASS: pytest, dry-run, mock batch and run.json verified.")
        print("This command verifies the logic core; real integration is tested separately with --real.")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"H3 TEST FAILED: command exited with code {exc.returncode}.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("H3 TEST FAILED: environment, installation or run record error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
