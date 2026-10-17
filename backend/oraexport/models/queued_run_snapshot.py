"""Validated settings captured when queuing an export."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QueuedRunSnapshot(BaseModel):
    """Validate complete settings for a newly queued export.

    Frozen fields and tuple schemas keep settings immutable until JSON persistence.
    RunSnapshot reads older records that may omit fields required for new runs.

    Attributes:
        name (str): Name of the export task.
        schema_name (str): Primary schema name targeted for export.
        directory (str): Oracle DIRECTORY object where dump files will be written.
        parallel (int): Number of parallel Data Pump execution worker threads.
        file_size_gb (int): Maximum size per dump file chunk in gigabytes.
        content (Literal['ALL', 'DATA_ONLY', 'METADATA_ONLY']): Export content filter mode.
        consistent_snapshot (bool): Whether to use flashback SCN for a consistent export.
        retention_days (int): Number of days to retain completed export dump files.
        cron (str): Cron schedule expression defining export recurrence.
        timezone (str): Timezone string used for schedule calculation.
        export_mode (Literal['SCHEMA', 'FULL']): Scope mode of the Data Pump export.
        schema_names (tuple[str, ...]): Tuple of schema names included in the export.
        target_name (str): Logical display name of the target database.
        dsn (str): Oracle Net connection string or Easy Connect DSN for target.
        credential_id (str): Foreign key to the stored export credential.
        database_name (str): Target database or container name.
        pdb_name (str | None): Pluggable database name or None if non-CDB.
        file_prefix (str): Prefix string used when generating dump file names.
        file_timezone (Literal['UTC']): Timezone standard used for dump file timestamps.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    schema_name: str
    directory: str
    parallel: int = Field(ge=1, le=64)
    file_size_gb: int = Field(ge=1, le=16384)
    content: Literal["ALL", "DATA_ONLY", "METADATA_ONLY"]
    consistent_snapshot: bool
    retention_days: int = Field(ge=1, le=36500)
    cron: str
    timezone: str
    export_mode: Literal["SCHEMA", "FULL"]
    schema_names: tuple[str, ...]
    target_name: str
    dsn: str
    credential_id: str
    database_name: str
    pdb_name: str | None
    file_prefix: str
    file_timezone: Literal["UTC"] = "UTC"
