from hashlib import sha256
import json
from pathlib import Path
import re

from .errors import (
    AmbiguousAssetError,
    ConfigurationError,
    DuplicateAssetError,
    MissingAssetError,
    WorkerError,
)
from .file_io import read_json, relative_path


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _file_hash(path: Path) -> str:
    try:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise MissingAssetError(f"Cannot read asset: {path.name}") from exc


def _normalized(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")


class AssetResolver:
    def __init__(
        self,
        manifest_path: Path,
        *,
        asset_roots: tuple[Path, ...] | None = None,
        index_path: Path | None = None,
    ):
        manifest = read_json(manifest_path, DuplicateAssetError)
        if not isinstance(manifest, dict) or not manifest:
            raise ConfigurationError("Asset manifest must be a non-empty object")
        self.assets = {}
        for name, value in manifest.items():
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise ConfigurationError("Asset names must be non-empty and trimmed")
            self.assets[name] = relative_path(manifest_path.parent, value)
        self.asset_roots = tuple(path.resolve() for path in (asset_roots or (manifest_path.parent,)))
        self.index_path = (index_path or manifest_path.parent / "asset_index.generated.json").resolve()
        self.index = self._load_index()

    def _load_index(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            value = read_json(self.index_path)
        except WorkerError:
            return {}
        assets = value.get("assets") if isinstance(value, dict) else None
        return assets if isinstance(assets, dict) else {}

    def _save_index(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "assets": self.index}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.index_path)

    def validate_manifest(self) -> None:
        for name, path in self.assets.items():
            if not path.is_file():
                raise MissingAssetError(f"Missing asset: {name}")
        for root in self.asset_roots:
            if not root.is_dir():
                raise ConfigurationError(
                    "Configured asset root does not exist",
                    details={"asset_root": str(root)},
                )

    def _cached(self, asset_id: str) -> tuple[Path, dict] | None:
        entry = self.index.get(asset_id)
        if not isinstance(entry, dict):
            return None
        path_value, expected_hash = entry.get("path"), entry.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected_hash, str):
            self.index.pop(asset_id, None)
            return None
        path = Path(path_value)
        if not path.is_file() or _file_hash(path) != expected_hash:
            self.index.pop(asset_id, None)
            self._save_index()
            return None
        return path, {
            "asset_id": asset_id,
            "source": "generated_index",
            "path": str(path),
            "sha256": expected_hash,
            "candidates": [str(path)],
        }

    def _scan(self, asset_id: str) -> tuple[Path, dict]:
        key = _normalized(asset_id)
        candidates = []
        for root in self.asset_roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
                    continue
                stem = _normalized(path.stem)
                if stem == key or stem.startswith(key + "-"):
                    candidates.append(path.resolve())
        candidates = sorted(set(candidates), key=lambda path: str(path).casefold())
        details = {"asset_id": asset_id, "candidates": [str(path) for path in candidates]}
        if not candidates:
            raise MissingAssetError(f"Missing asset: {asset_id}", details=details)
        if len(candidates) != 1:
            raise AmbiguousAssetError(f"Ambiguous asset: {asset_id}", details=details)
        path = candidates[0]
        digest = _file_hash(path)
        self.index[asset_id] = {"path": str(path), "sha256": digest}
        self._save_index()
        return path, {
            "asset_id": asset_id,
            "source": "unique_scan",
            "path": str(path),
            "sha256": digest,
            "candidates": [str(path)],
        }

    def _resolve_one(self, asset_id: str) -> tuple[Path, dict]:
        manual = self.assets.get(asset_id)
        if manual is not None:
            if not manual.is_file():
                raise MissingAssetError(f"Missing asset: {asset_id}")
            digest = _file_hash(manual)
            return manual, {
                "asset_id": asset_id,
                "source": "manual_manifest",
                "path": str(manual),
                "sha256": digest,
                "candidates": [str(manual)],
            }
        cached = self._cached(asset_id)
        return cached if cached is not None else self._scan(asset_id)

    def resolve_detailed(self, references: dict[str, str]) -> tuple[dict[str, Path], dict]:
        resolved, audit = {}, {}
        for slot, asset_id in references.items():
            path, entry = self._resolve_one(asset_id)
            resolved[slot] = path
            audit[slot] = entry
        return resolved, audit

    def resolve(self, references: dict[str, str]) -> dict[str, Path]:
        return self.resolve_detailed(references)[0]
