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
        return Config(
            base_url.rstrip("/"),
            relative_path(path.parent, workflow["template"]),
            mapping,
            relative_path(path.parent, paths["asset_manifest"]),
            relative_path(path.parent, paths["output_root"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Missing or invalid configuration fields") from exc
