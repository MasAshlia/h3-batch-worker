"""Structured, path-safe errors for CLI and run records."""


class WorkerError(Exception):
    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "message": str(self)}


class MissingDurationError(WorkerError):
    pass


class MissingReferenceError(WorkerError):
    pass


class InvalidPromptFormatError(WorkerError):
    pass


class MissingAssetError(WorkerError):
    pass


class DuplicateAssetError(WorkerError):
    pass


class ConfigurationError(WorkerError):
    pass


class WorkflowError(WorkerError):
    pass


class ComfyClientError(WorkerError):
    pass


class CompletionTimeoutError(ComfyClientError):
    pass
