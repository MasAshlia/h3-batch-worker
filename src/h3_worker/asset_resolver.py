from pathlib import Path

from .errors import ConfigurationError, DuplicateAssetError, MissingAssetError
from .file_io import read_json, relative_path


class AssetResolver:
    def __init__(self, manifest_path: Path):
        manifest = read_json(manifest_path, DuplicateAssetError)
        if not isinstance(manifest, dict) or not manifest:
            raise ConfigurationError("Asset manifest must be a non-empty object")
        self.assets = {}
        for name, value in manifest.items():
            if not name.strip() or name != name.strip():
                raise ConfigurationError("Asset names must be non-empty and trimmed")
            self.assets[name] = relative_path(manifest_path.parent, value)

    def validate_manifest(self) -> None:
        # Validate unused entries too: a broken asset manifest is a batch error.
        for name, path in self.assets.items():
            if not path.is_file():
                raise MissingAssetError(f"Missing asset: {name}")

    def resolve(self, references: dict[str, str]) -> dict[str, Path]:
        resolved = {}
        for key, name in references.items():
            path = self.assets.get(name)
            if path is None or not path.is_file():
                raise MissingAssetError(f"Missing asset: {name}")
            resolved[key] = path
        return resolved
