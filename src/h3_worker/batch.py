from pathlib import Path
import re

from .asset_resolver import AssetResolver
from .config import Config
from .errors import ConfigurationError, WorkerError
from .file_io import read_json, relative_path
from .models import BatchItem, BatchReport, CompiledTask, TaskStatus
from .parser import parse_prompt
from .validator import validate_task
from .workflow_patcher import WorkflowPatcher


def compile_batch(manifest_path: Path, config: Config) -> tuple[BatchReport, WorkflowPatcher]:
    manifest = read_json(manifest_path)
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
    resolver = AssetResolver(config.asset_manifest)
    patcher = WorkflowPatcher(read_json(config.template_path), config.mapping)
    # Global broken manifests/templates fail the entire batch before any submission.
    resolver.validate_manifest()
    items = []
    for shot in manifest["shots"]:
        item = BatchItem(shot["shot_id"])
        try:
            path = relative_path(manifest_path.parent, shot.get("prompt_file"))
            try:
                prompt = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise ConfigurationError("Cannot read Prompt input file") from exc
            task = parse_prompt(
                shot["shot_id"],
                prompt,
                shot.get("seed"),
                duration_seconds=shot.get("duration"),
                references=shot.get("references"),
            )
            validate_task(task)
            assets = resolver.resolve(task.reference_slots)
            workflow = patcher.patch(task, {slot: path.as_posix() for slot, path in assets.items()})
            task.status = TaskStatus.VALIDATED
            item.compiled = CompiledTask(task, assets, workflow)
        except WorkerError as exc:
            item.errors.append(exc.to_dict())
        items.append(item)
    return BatchReport(manifest["episode"], items), patcher
