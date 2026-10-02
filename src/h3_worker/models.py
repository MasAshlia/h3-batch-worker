from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRY = "RETRY"


@dataclass
class ShotTask:
    shot_id: str
    prompt: str
    duration_seconds: int
    steps: int
    references: dict[str, str]
    seed: int | None = None
    status: TaskStatus = TaskStatus.PENDING

    @property
    def reference_slots(self) -> dict[str, str]:
        return {
            f"ref_image_{int(key.removeprefix('image_')) - 1}": name
            for key, name in self.references.items()
        }


@dataclass
class CompiledTask:
    task: ShotTask
    assets: dict[str, Path]
    workflow: dict


@dataclass
class BatchItem:
    shot_id: str
    compiled: CompiledTask | None = None
    errors: list[dict] = field(default_factory=list)


@dataclass
class BatchReport:
    episode: str
    items: list[BatchItem]

    @property
    def ready(self) -> bool:
        return bool(self.items) and all(
            item.compiled is not None and not item.errors for item in self.items
        )

    @property
    def tasks(self) -> list[CompiledTask]:
        return [item.compiled for item in self.items if item.compiled is not None]
