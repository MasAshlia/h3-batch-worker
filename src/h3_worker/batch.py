from pathlib import Path
import re

from .asset_resolver import AssetResolver
from .config import Config
from .errors import ConfigurationError, WorkerError
from .file_io import read_json, read_manifest, relative_path
from .models import BatchItem, BatchReport, CompiledTask, TaskStatus
from .parser import parse_prompt, parse_shot_identity
from .validator import validate_task
from .workflow_patcher import WorkflowPatcher


def _build_components(config: Config, max_reference_images: int):
    resolver = AssetResolver(
        config.asset_manifest,
        asset_roots=config.asset_roots,
        index_path=config.asset_index,
    )
    patcher = WorkflowPatcher(
        read_json(config.template_path), config.mapping,
        max_reference_images=max_reference_images,
    )
    resolver.validate_manifest()
    return resolver, patcher


def _compile_task(
    shot_id: str,
    prompt: str,
    seed,
    resolver: AssetResolver,
    patcher: WorkflowPatcher,
    *,
    source_path: Path | None = None,
    duration_seconds=None,
    references=None,
) -> CompiledTask:
    task = parse_prompt(
        shot_id,
        prompt,
        seed,
        duration_seconds=duration_seconds,
        references=references,
    )
    validate_task(task)
    assets, asset_audit = resolver.resolve_detailed(task.reference_slots)
    workflow, workflow_mapping = patcher.patch_with_mapping(
        task, {slot: path.as_posix() for slot, path in assets.items()},
    )
    task.status = TaskStatus.VALIDATED
    return CompiledTask(
        task,
        assets,
        workflow,
        source_path=source_path,
        asset_audit=asset_audit,
        workflow_mapping=workflow_mapping,
    )


def compile_batch(
    manifest_path: Path, config: Config, *, max_reference_images: int = 9,
) -> tuple[BatchReport, WorkflowPatcher]:
    manifest = read_manifest(manifest_path)
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("episode"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", manifest["episode"])
            or not isinstance(manifest.get("shots"), list) or not manifest["shots"]):
        raise ConfigurationError("Episode manifest requires an episode id and non-empty shots list")
    shot_ids = set()
    for shot in manifest["shots"]:
        if (not isinstance(shot, dict) or not isinstance(shot.get("shot_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", shot["shot_id"])):
            raise ConfigurationError("Invalid shot_id in episode manifest")
        if shot["shot_id"] in shot_ids:
            raise ConfigurationError("Duplicate shot_id in episode manifest")
        shot_ids.add(shot["shot_id"])
    resolver, patcher = _build_components(config, max_reference_images)
    items = []
    for shot in manifest["shots"]:
        item = BatchItem(shot["shot_id"])
        try:
            inline_prompt = shot.get("prompt")
            prompt_file = shot.get("prompt_file")
            if isinstance(inline_prompt, str) and prompt_file is None:
                prompt = inline_prompt
            elif inline_prompt is None and isinstance(prompt_file, str):
                path = relative_path(manifest_path.parent, prompt_file)
                try:
                    prompt = path.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError) as exc:
                    raise ConfigurationError("Cannot read Prompt input file") from exc
            else:
                raise ConfigurationError("Shot requires exactly one of prompt or prompt_file")
            item.compiled = _compile_task(
                shot["shot_id"], prompt, shot.get("seed"), resolver, patcher,
                source_path=path if inline_prompt is None else manifest_path,
                duration_seconds=shot.get("duration"),
                references=shot.get("references"),
            )
        except WorkerError as exc:
            item.errors.append(exc.to_dict())
        items.append(item)
    return BatchReport(manifest["episode"], items), patcher


def compile_episode_folder(
    folder_path: Path, config: Config, *, max_reference_images: int = 9,
) -> tuple[BatchReport, WorkflowPatcher]:
    folder_match = re.fullmatch(r"(EP[0-9]+)\.(?:ready|processing)", folder_path.name, re.IGNORECASE)
    if not folder_path.is_dir() or folder_match is None:
        raise ConfigurationError("Episode folder must be named EP<number>.ready or .processing")
    expected_episode = folder_match[1].upper()
    prompt_files = sorted(folder_path.glob("*.txt"), key=lambda path: path.name.casefold())
    if not prompt_files:
        raise ConfigurationError("Episode folder contains no TXT Prompt files")

    parsed = []
    seen = set()
    for path in prompt_files:
        try:
            prompt = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("Cannot read Prompt input file") from exc
        episode, shot_id, order = parse_shot_identity(prompt)
        if episode != expected_episode:
            raise ConfigurationError(
                "Prompt episode does not match ready folder",
                details={"file": path.name, "expected": expected_episode, "actual": episode},
            )
        if shot_id in seen:
            raise ConfigurationError(f"Duplicate shot_id in episode folder: {shot_id}")
        seen.add(shot_id)
        parsed.append((order, shot_id, path, prompt))
    parsed.sort(key=lambda value: value[0])

    resolver, patcher = _build_components(config, max_reference_images)
    items = []
    for _, shot_id, path, prompt in parsed:
        item = BatchItem(shot_id)
        try:
            item.compiled = _compile_task(
                shot_id, prompt, None, resolver, patcher, source_path=path,
            )
        except WorkerError as exc:
            item.errors.append(exc.to_dict())
        items.append(item)
    return BatchReport(expected_episode, items), patcher
