"""Domain errors mapped to JSON responses by the FastAPI application."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Base class for errors that become structured API responses."""

    status_code = 400
    code = "api_error"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class ConflictError(ApiError):
    status_code = 409
    code = "conflict"


class VersionConflictError(ConflictError):
    """The area changed after the curator read its version."""

    code = "area_version_conflict"


class BatchValidationError(ConflictError):
    """The batch is well-formed JSON but violates domain rules."""

    code = "batch_rejected"


class CycleDetectedError(ConflictError):
    """Applying the batch would make the whole-site graph cyclic."""

    code = "cycle_detected"


class StorageUnavailableError(ApiError):
    """SQLite could not obtain the write lock in time; nothing changed."""

    status_code = 503
    code = "storage_unavailable"
