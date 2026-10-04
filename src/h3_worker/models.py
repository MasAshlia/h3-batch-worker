from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    UPLOADING = "UPLOADING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
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
    original_prompt: str | None = None
    raw_duration_seconds: float | None = None
    transformations: list[str] = field(default_factory=list)

    @property
    def reference_slots(self) -> dict[str, str]:
        return {
            f"ref_image_{int(key.removeprefix('image_')) - 1}": name
            for key, name in self.references.items()
        }

    @property
    def expected_frame_count(self) -> int:
        frames = max(5, round(self.duration_seconds * 24))
        return frames + (5 - frames % 17) % 17

    @property
    def expected_output_seconds(self) -> float:
        return self.expected_frame_count / 24


@dataclass
class CompiledTask:
    task: ShotTask
    assets: dict[str, Path]
    workflow: dict
    source_path: Path | None = None
    asset_audit: dict = field(default_factory=dict)
    workflow_mapping: dict = field(default_factory=dict)


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
