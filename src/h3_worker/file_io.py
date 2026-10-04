import json
from pathlib import Path

import yaml

from .errors import ConfigurationError, WorkerError


def read_json(path: Path, duplicate_error: type[WorkerError] = ConfigurationError):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                # Do not let json.load silently discard a conflicting definition.
                raise duplicate_error("Duplicate JSON key in manifest or template")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("Cannot read JSON input file") from exc
    except (ValueError, RecursionError) as exc:
        raise ConfigurationError("Invalid JSON input file") from exc


class _UniqueYamlLoader(yaml.SafeLoader):
    pass


def _unique_yaml_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_yaml_mapping,
)


def read_manifest(path: Path):
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return read_json(path)
    try:
        return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueYamlLoader)
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("Cannot read YAML input file") from exc
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigurationError("Invalid YAML input file") from exc


def relative_path(base: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("Configured path must be a non-empty string")
    return (base / Path(value)).resolve()
