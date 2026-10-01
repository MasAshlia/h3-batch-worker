import argparse
import logging
from pathlib import Path

from .batch import compile_batch
from .comfy_client import MockComfyClient
from .config import load_config
from .errors import ConfigurationError, WorkerError
from .models import BatchReport
from .queue_manager import QueueManager


def _print_report(report: BatchReport) -> None:
    print(f"{report.episode}\nTasks: {len(report.items)}")
    for item in report.items:
        print(f"\n{item.shot_id}")
        if item.errors:
            for error in item.errors:
                print(f"ERROR {error['type']}: {error['message']}")
            continue
        compiled = item.compiled
        task = compiled.task
        print(f"duration: {task.duration_seconds}s\nsteps: {task.steps}")
        for key, name in task.references.items():
            slot = f"ref_image_{int(key.removeprefix('image_')) - 1}"
            print(f"@{key} -> {slot} -> {name} -> {compiled.assets[slot].name}")
        print("workflow: PASS")
    print(f"\nSUMMARY\n{len(report.items)} tasks\n{len(report.tasks)} valid")
    print(f"{len(report.items) - len(report.tasks)} invalid")
    print("READY FOR SUBMISSION" if report.ready else "BATCH NOT READY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H3 Batch Worker V0.1 Mac Logic PoC")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("manifest", type=Path)
        sub.add_argument("--config", type=Path, help="Defaults to config.yaml, then config.example.yaml in cwd")
        if command == "run":
            sub.add_argument("--mock", action="store_true", required=True, help="V0.1 supports mock runs only")
            sub.add_argument("--mock-fail", action="append", default=[], metavar="SHOT_ID",
                             help="Fail every attempt of this shot (repeatable)")
            sub.add_argument("--mock-fail-once", action="append", default=[], metavar="SHOT_ID",
                             help="Fail the first attempt only (repeatable)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        config_path = args.config or (Path("config.yaml") if Path("config.yaml").exists()
                                     else Path("config.example.yaml"))
        config = load_config(config_path.resolve())
        report, patcher = compile_batch(args.manifest.resolve(), config)
        if patcher.is_test_fixture:
            print("TEST FIXTURE ONLY — workflow PASS checks local structure, not real H3 executability")
        _print_report(report)
        if not report.ready:
            return 1
        if args.command == "run":
            fail_ids = set(args.mock_fail) | set(args.mock_fail_once)
            if not fail_ids <= {item.shot_id for item in report.items}:
                raise ConfigurationError("Mock failure shot_id does not exist in batch")
            if set(args.mock_fail) & set(args.mock_fail_once):
                raise ConfigurationError("Conflicting mock failure options for the same shot")
            failures = {shot: 1 for shot in args.mock_fail_once}
            failures.update({shot: -1 for shot in args.mock_fail})
            run_path, record = QueueManager(MockComfyClient(failures=failures), patcher,
                                            config.output_root).run(report)
            print(f"\nBatch complete\nSuccess: {record['summary']['success']}\nFailed: {record['summary']['failed']}")
            # Show only batch-relative location, never the user's absolute path.
            print(f"Run record: {run_path.parent.name}/run.json (under configured output_root)")
            return 1 if record["summary"]["failed"] else 0
        return 0
    except WorkerError as exc:
        error = exc.to_dict()
        print(f"ERROR {error['type']}: {error['message']}\nBATCH NOT READY")
        return 1
    except (OSError, ValueError):
        print("ERROR ConfigurationError: Cannot access configured files or write run output\nBATCH NOT READY")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
