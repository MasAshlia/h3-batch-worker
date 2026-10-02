import json
from pathlib import Path

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


def relative_path(base: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("Configured path must be a non-empty string")
    return (base / Path(value)).resolve()
