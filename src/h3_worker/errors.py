"""Structured, path-safe errors for CLI and run records."""


class WorkerError(Exception):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}

    def to_dict(self) -> dict:
        result = {"type": type(self).__name__, "message": str(self)}
        if self.details:
            result["details"] = self.details
        return result


class MissingDurationError(WorkerError):
    pass


class MissingReferenceError(WorkerError):
    pass


class InvalidPromptFormatError(WorkerError):
    pass


class MissingAssetError(WorkerError):
    pass


class AmbiguousAssetError(WorkerError):
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


class UnknownSubmissionError(ComfyClientError):
    pass
