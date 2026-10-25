"""BackupFileResponse Pydantic model."""

from .response_model import ResponseModel


class BackupFileResponse(ResponseModel):
    """Owned export artifact file record.

    Attributes:
        id (str): Unique identifier for the export file record.
        directory (str): Directory object name or filesystem path where file resides.
        filename (str): Base filename of the export artifact.
        kind (str): Artifact kind, such as dump, log, or sql.
        size_bytes (int | None): File size in bytes, or None if unmeasured.
        deleted_at (str | None): ISO UTC timestamp when file was deleted, or None if active.
        cleanup_attempts (int): Number of cleanup or deletion attempts executed.
        cleanup_error (str | None): Last error message encountered during cleanup, if any.
    """

    id: str
    directory: str
    filename: str
    kind: str
    size_bytes: int | None = None
    deleted_at: str | None = None
    cleanup_attempts: int = 0
    cleanup_error: str | None = None
