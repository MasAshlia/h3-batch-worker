import argparse
import json
import logging
from pathlib import Path
import sys
import time

from .batch import compile_batch, compile_episode_folder
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
            mapping = compiled.workflow_mapping.get(slot, {})
            node_id = mapping.get("load_node")
            load_target = f" -> LoadImage node {node_id}" if node_id is not None else ""
            print(f"@{key} -> {name} -> {compiled.assets[slot].name} -> {slot}{load_target}")
        print("Workflow: PASS")
    print(f"\nSUMMARY\n{len(report.items)} tasks\n{len(report.tasks)} valid")
    print(f"{len(report.items) - len(report.tasks)} invalid")
    print("READY FOR SUBMISSION" if report.ready else "BATCH NOT READY")


def _run_real_batch(config, report, patcher, client, *, wait_for_initial_idle: bool):
    if patcher.is_test_fixture:
        raise ConfigurationError("Real run refuses TEST FIXTURE workflows")
    if not any(
        isinstance(node, dict)
        and "video" in (str(node.get("class_type", "")) + str(node.get("_meta", {}))).lower()
        and "save" in str(node.get("class_type", "")).lower()
        for node in patcher.template.values()
    ):
        raise ConfigurationError("Real workflow has no video output node")
    stats = client.get_system_stats()
    if not isinstance(stats.get("system"), dict) or not isinstance(stats.get("devices"), list):
        raise ConfigurationError("ComfyUI system_stats preflight failed")
    if wait_for_initial_idle:
        client.wait_for_idle()
    else:
        queue = client.get_queue()
        if queue["queue_running"] or queue["queue_pending"]:
            raise ConfigurationError("ComfyUI queue must be idle before the real test")
    return QueueManager(
        client,
        patcher,
        config.output_root,
        max_attempts=1,
        require_queue_confirmation=True,
        require_video_output=True,
        require_idle_before_each=True,
        stop_on_failure=True,
        debug_root=config.debug_root,
        comfy_output=config.comfy_output,
        ffmpeg_executable=config.ffmpeg_executable,
    ).run(report)


def _move_inbox_item(path: Path, inbox: Path, state: str) -> Path:
    folder = inbox / state
    folder.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        base = path.name.removesuffix(".processing").removesuffix(".ready")
        target = folder / f"{base}.{state}"
    else:
        extension = path.suffix
        base = path.name.removesuffix(f".processing{extension}")
        base = base.removesuffix(f".ready{extension}")
        target = folder / f"{base}.{state}{extension}"
    if target.exists():
        suffix = "" if path.is_dir() else path.suffix
        target = folder / f"{base}.{state}.{time.time_ns()}{suffix}"
    return path.replace(target)


def _claim_inbox_item(source: Path) -> Path:
    if source.is_dir():
        processing = source.with_name(source.name.removesuffix(".ready") + ".processing")
    else:
        extension = source.suffix
        base = source.name.removesuffix(f".ready{extension}")
        processing = source.with_name(f"{base}.processing{extension}")
    if processing.exists():
        raise ConfigurationError("Matching processing input already exists")
    return source.replace(processing)


def _compile_inbox_item(path: Path, config, max_reference_images: int):
    if path.is_dir():
        return compile_episode_folder(
            path, config, max_reference_images=max_reference_images,
        )
    return compile_batch(path, config, max_reference_images=max_reference_images)


def _write_diagnostics(path: Path, error: dict, client, *, run_record: dict | None = None) -> None:
    diagnostics = {
        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "category": error.get("type", "UnknownError"),
        "error": error,
        "repairs": list(getattr(client, "repair_events", [])),
        "safe_manual_action": (
            "Correct the listed Prompt, asset mapping, or ComfyUI condition, then rename or copy "
            "the episode back to EP<number>.ready. The watcher will not guess assets or resubmit it."
        ),
    }
    if run_record is not None:
        diagnostics["run"] = {
            "batch_id": run_record.get("batch_id"),
            "summary": run_record.get("summary"),
            "shots": [{
                "shot_id": shot.get("shot_id"),
                "prompt_id": shot.get("prompt_id"),
                "status": shot.get("status"),
                "error": shot.get("error"),
                "asset_mapping": shot.get("asset_mapping"),
            } for shot in run_record.get("shots", [])],
        }
    if path.is_dir():
        destination = path / "diagnostics.json"
    else:
        destination = path.with_name(path.stem + ".diagnostics.json")
    destination.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")


def _watch_inbox(args, config) -> int:
    if args.poll_interval <= 0:
        raise ConfigurationError("--poll-interval must be positive")
    inbox = args.inbox.resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    client = RealComfyClient(config.base_url) if args.real else MockComfyClient()
    failures = 0
    print(f"Watching inbox: {inbox.name} (EP*.ready folders and *.ready.json/yaml)")
    try:
        while True:
            ready_files = list(
                path for pattern in ("*.ready.json", "*.ready.yaml", "*.ready.yml")
                for path in inbox.glob(pattern)
            )
            ready_folders = [
                path for path in inbox.glob("EP*.ready") if path.is_dir()
            ]
            ready_items = sorted(ready_files + ready_folders, key=lambda path: path.name.casefold())
            for source in ready_items:
                succeeded = False
                record = None
                failure_error = None
                processing = None
                try:
                    reference_limit = client.get_h3_reference_limit() if args.real else 9
                    report, patcher = _compile_inbox_item(
                        source, config, reference_limit,
                    )
                    if (args.real and not report.ready
                            and any(error.get("type") == "WorkflowError"
                                    for item in report.items for error in item.errors)):
                        refreshed_limit = client.get_h3_reference_limit()
                        client.repair_events.append({
                            "type": "schema_refresh", "old_limit": reference_limit,
                            "new_limit": refreshed_limit,
                        })
                        report, patcher = _compile_inbox_item(
                            source, config, refreshed_limit,
                        )
                    if patcher.is_test_fixture:
                        print("TEST FIXTURE ONLY — workflow PASS checks local structure, not real H3 executability")
                    _print_report(report, patcher)
                    if not report.ready:
                        raise ConfigurationError(
                            "BATCH NOT READY: all tasks must pass preflight",
                            details={
                                "shots": [
                                    {"shot_id": item.shot_id, "errors": item.errors}
                                    for item in report.items if item.errors
                                ],
                            },
                        )
                    processing = _claim_inbox_item(source)
                    if args.real:
                        run_path, record = _run_real_batch(
                            config, report, patcher, client, wait_for_initial_idle=True,
                        )
                    else:
                        run_path, record = QueueManager(
                            client,
                            patcher,
                            config.output_root,
                            max_attempts=1,
                            stop_on_failure=True,
                        ).run(report)
                    succeeded = record["summary"]["failed"] == 0
                    if not succeeded:
                        failed_shot = next(
                            (shot for shot in record["shots"] if shot["status"] == "FAILED"), None,
                        )
                        failure_error = (failed_shot or {}).get("error") or {
                            "type": "EpisodeFailed", "message": "Episode generation failed",
                        }
                    print(
                        f"Episode complete: {report.episode} "
                        f"({record['summary']['success']} success, {record['summary']['failed']} failed)"
                    )
                    print(f"Run record: {run_path.parent.name}/run.json")
                except WorkerError as exc:
                    error = exc.to_dict()
                    failure_error = error
                    print(f"ERROR {error['type']}: {error['message']}")
                except (OSError, ValueError):
                    failure_error = {
                        "type": "ConfigurationError",
                        "message": "Cannot process inbox episode",
                    }
                    print("ERROR ConfigurationError: Cannot process inbox episode")
                if not succeeded:
                    try:
                        _write_diagnostics(
                            processing or source,
                            failure_error or {"type": "EpisodeFailed", "message": "Unknown failure"},
                            client,
                            run_record=record,
                        )
                    except OSError:
                        print("ERROR ConfigurationError: Cannot write diagnostics.json")
                destination = _move_inbox_item(
                    processing or source, inbox, "done" if succeeded else "failed",
                )
                print(f"Moved input to {destination.parent.name}/{destination.name}")
                if not succeeded:
                    failures += 1
            if args.once:
                return 1 if failures else 0
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("Watcher stopped")
        return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="H3 Batch Worker guarded real integration")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("manifest", type=Path)
        sub.add_argument("--config", type=Path, help="Defaults to config.yaml, then config.example.yaml in cwd")
        if command == "run":
            mode = sub.add_mutually_exclusive_group(required=True)
            mode.add_argument("--mock", action="store_true")
            mode.add_argument("--real", action="store_true", help="Submit to real ComfyUI (one shot by default)")
            sub.add_argument(
                "--max-real-shots", type=int, metavar="N",
                help="With --real only: explicitly allow up to N sequential shots (hard maximum 3)",
            )
            sub.add_argument("--mock-fail", action="append", default=[], metavar="SHOT_ID",
                             help="Fail every attempt of this shot (repeatable)")
            sub.add_argument("--mock-fail-once", action="append", default=[], metavar="SHOT_ID",
                             help="Fail the first attempt only (repeatable)")
    watch = commands.add_parser(
        "watch", help="Continuously process EP*.ready folders and legacy *.ready.json/yaml files",
    )
    watch.add_argument("inbox", type=Path)
    watch.add_argument("--config", type=Path, help="Defaults to config.yaml, then config.example.yaml in cwd")
    watch_mode = watch.add_mutually_exclusive_group(required=True)
    watch_mode.add_argument("--mock", action="store_true")
    watch_mode.add_argument("--real", action="store_true")
    watch.add_argument("--poll-interval", type=float, default=2.0, metavar="SECONDS")
    watch.add_argument("--once", action="store_true", help="Process current files and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        if args.command == "run" and args.max_real_shots is not None:
            if not args.real:
                raise ConfigurationError("--max-real-shots can only be used with --real")
            if not 1 <= args.max_real_shots <= 3:
                raise ConfigurationError("--max-real-shots must be between 1 and 3")
        config_path = args.config or (Path("config.yaml") if Path("config.yaml").exists()
                                     else Path("config.example.yaml"))
        config = load_config(config_path.resolve())
        if args.command == "watch":
            return _watch_inbox(args, config)
        max_references = 9
        client = None
        if args.command == "run" and args.real:
            client = RealComfyClient(config.base_url)
            max_references = client.get_h3_reference_limit()
        if args.manifest.is_dir():
            report, patcher = compile_episode_folder(
                args.manifest.resolve(), config, max_reference_images=max_references,
            )
        else:
            report, patcher = compile_batch(
                args.manifest.resolve(), config, max_reference_images=max_references,
            )
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
                real_limit = args.max_real_shots or 1
                if len(report.tasks) > real_limit:
                    raise ConfigurationError(
                        f"Real integration allows at most {real_limit} shot(s) for this invocation"
                    )
                if patcher.is_test_fixture:
                    raise ConfigurationError("Real run refuses TEST FIXTURE workflows")
                if args.mock_fail or args.mock_fail_once:
                    raise ConfigurationError("Mock failure options cannot be used with --real")
                run_path, record = _run_real_batch(
                    config, report, patcher, client, wait_for_initial_idle=False,
                )
                print(
                    f"\nReal run complete\nSuccess: {record['summary']['success']}"
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
