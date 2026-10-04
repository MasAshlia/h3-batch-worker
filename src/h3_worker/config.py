from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .errors import ConfigurationError
from .file_io import relative_path


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError("Configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class Config:
    base_url: str
    template_path: Path
    mapping: dict
    asset_manifest: Path
    output_root: Path
    debug_root: Path
    comfy_output: Path | None
    asset_roots: tuple[Path, ...]
    asset_index: Path


def load_config(path: Path) -> Config:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ConfigurationError("Cannot read valid YAML configuration") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration must be an object")
    try:
        comfy, workflow, paths = raw["comfyui"], raw["workflow"], raw["paths"]
        base_url, mapping = comfy["base_url"], workflow["mapping"]
        if not isinstance(base_url, str) or not isinstance(mapping, dict):
            raise ConfigurationError("Invalid base_url or workflow mapping")
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise ConfigurationError("base_url must be an HTTP(S) URL without credentials")
        # Force validation of malformed port syntax without logging the URL.
        url.port
        output_root = relative_path(path.parent, paths["output_root"])
        debug_root = (
            relative_path(path.parent, paths["debug_root"])
            if paths.get("debug_root") else output_root.parent / "debug"
        )
        asset_manifest = relative_path(path.parent, paths["asset_manifest"])
        roots = paths.get("asset_roots")
        if roots is None:
            asset_roots = (asset_manifest.parent,)
        elif isinstance(roots, list) and roots and all(isinstance(item, str) for item in roots):
            asset_roots = tuple(relative_path(path.parent, item) for item in roots)
        else:
            raise ConfigurationError("paths.asset_roots must be a non-empty list")
        return Config(
            base_url=base_url.rstrip("/"),
            template_path=relative_path(path.parent, workflow["template"]),
            mapping=mapping,
            asset_manifest=asset_manifest,
            output_root=output_root,
            debug_root=debug_root,
            comfy_output=(
                relative_path(path.parent, paths["comfy_output"])
                if paths.get("comfy_output") else None
            ),
            asset_roots=asset_roots,
            asset_index=(
                relative_path(path.parent, paths["asset_index"])
                if paths.get("asset_index") else asset_manifest.parent / "asset_index.generated.json"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Missing or invalid configuration fields") from exc
