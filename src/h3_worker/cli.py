import argparse
import json
import logging
from pathlib import Path
import sys

from .batch import compile_batch
from .comfy_client import MockComfyClient, RealComfyClient
from .config import load_config
from .errors import ConfigurationError, WorkerError
from .models import BatchReport
from .queue_manager import QueueManager


def _print_report(report: BatchReport, patcher=None) -> None:
    print(f"{report.episode}\nTasks: {len(report.items)}")
    for item in report.items:
        print(f"\n{item.shot_id}")
        if item.errors:
            for error in item.errors:
                print(f"ERROR {error['type']}: {error['message']}")
            continue
        compiled = item.compiled
        task = compiled.task
        print(f"Prompt: PASS\nDuration: {task.duration_seconds}\nSteps: {task.steps}")
        for key, name in task.references.items():
            slot = f"ref_image_{int(key.removeprefix('image_')) - 1}"
            target = patcher.reference_targets.get(slot) if patcher is not None else None
            load_target = f" -> LoadImage node {target[0]}" if target is not None else ""
            print(f"@{key} -> {name} -> {compiled.assets[slot].name} -> {slot}{load_target}")
        print("Workflow: PASS")
    print(f"\nSUMMARY\n{len(report.items)} tasks\n{len(report.tasks)} valid")
    print(f"{len(report.items) - len(report.tasks)} invalid")
    print("READY FOR SUBMISSION" if report.ready else "BATCH NOT READY")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="H3 Batch Worker V0.2 single-shot integration")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("manifest", type=Path)
        sub.add_argument("--config", type=Path, help="Defaults to config.yaml, then config.example.yaml in cwd")
        if command == "run":
            mode = sub.add_mutually_exclusive_group(required=True)
            mode.add_argument("--mock", action="store_true")
            mode.add_argument("--real", action="store_true", help="Submit exactly one shot to real ComfyUI")
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
        _print_report(report, patcher)
        if not report.ready:
            return 1
        if args.command == "dry-run" and not patcher.is_test_fixture:
            config.debug_root.mkdir(parents=True, exist_ok=True)
            for compiled in report.tasks:
                path = config.debug_root / f"{compiled.task.shot_id}_patched_local.json"
                path.write_text(json.dumps(compiled.workflow, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Patched local workflow: saved under configured debug_root")
        if args.command == "run":
            if args.real:
                if len(report.tasks) != 1:
                    raise ConfigurationError("Real integration V0.2 currently allows exactly one shot")
                if patcher.is_test_fixture:
                    raise ConfigurationError("Real run refuses TEST FIXTURE workflows")
                if args.mock_fail or args.mock_fail_once:
                    raise ConfigurationError("Mock failure options cannot be used with --real")
                if not any(
                    isinstance(node, dict)
                    and "video" in (str(node.get("class_type", "")) + str(node.get("_meta", {}))).lower()
                    and "save" in str(node.get("class_type", "")).lower()
                    for node in patcher.template.values()
                ):
                    raise ConfigurationError("Real workflow has no video output node")
                client = RealComfyClient(config.base_url)
                stats = client.get_system_stats()
                if not isinstance(stats.get("system"), dict) or not isinstance(stats.get("devices"), list):
                    raise ConfigurationError("ComfyUI system_stats preflight failed")
                queue = client.get_queue()
                if queue["queue_running"] or queue["queue_pending"]:
                    raise ConfigurationError("ComfyUI queue must be idle before the real single-shot test")
                run_path, record = QueueManager(
                    client,
                    patcher,
                    config.output_root,
                    max_attempts=1,
                    require_queue_confirmation=True,
                    require_video_output=True,
                    debug_root=config.debug_root,
                    comfy_output=config.comfy_output,
                ).run(report)
                print(
                    f"\nReal single-shot complete\nSuccess: {record['summary']['success']}"
                    f"\nFailed: {record['summary']['failed']}"
                )
                print(f"Run record: {run_path.parent.name}/run.json (under configured output_root)")
                return 1 if record["summary"]["failed"] else 0
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
