"""BackupFile metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import uid


class BackupFile(BaseModel):
    """Exact owned filenames; cleanup never enumerates arbitrary directory contents.

    Maps to the backup_files table. Records verified dump, log, and SQL files produced
    by an export run to ensure surgical file retention deletion.

    Attributes:
        id (str): Unique identifier for the export file record.
        run_id (str): Identifier of the export run that produced the file.
        directory (str): Directory object name or filesystem path where file resides.
        filename (str): Base filename of the export artifact.
        kind (str): Artifact kind, such as dump, log, or sql.
        size_bytes (int | None): File size in bytes, or None if unmeasured.
        deleted_at (datetime | None): UTC timestamp when file was deleted, or None if active.
        cleanup_attempts (int): Number of cleanup or deletion attempts executed.
        cleanup_error (str | None): Last error message encountered during cleanup, if any.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "backup_files"
    id: str = Field(default_factory=uid)
    run_id: str
    directory: str
    filename: str
    kind: str
    size_bytes: int | None = None
    deleted_at: datetime | None = None
    cleanup_attempts: int = 0
    cleanup_error: str | None = None
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ("cleanup_error",)
