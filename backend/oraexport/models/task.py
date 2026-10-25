"""Task metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import uid


class Task(BaseModel):
    """Durable schema-selection or full-target export policy.

    Maps to the tasks table. Encapsulates export options, cron schedule, retention
    period, and concurrency parameters for scheduled and manual export runs.

    Attributes:
        id (str): Unique task policy identifier.
        name (str): Descriptive task policy name.
        target_id (str): Associated target database service identifier.
        schema_name (str): Primary or legacy single schema name.
        directory (str): Oracle directory object name used for dump files.
        enabled (bool): Flag indicating whether scheduled execution is active.
        cron (str): Standard five-field cron schedule expression.
        timezone (str): IANA timezone identifier for cron execution scheduling.
        retention_days (int): Number of days to retain completed export artifacts.
        parallel (int): Data Pump export parallel worker process count.
        file_size_gb (int): Maximum dump file piece size in gigabytes.
        content (str): Export content filter (ALL, DATA_ONLY, or METADATA_ONLY).
        consistent_snapshot (bool): Flag indicating whether export uses flashback snapshot.
        next_run_at (datetime | None): Scheduled timestamp for the next automated execution.
        export_mode (str): Scope of export operation (SCHEMA or FULL).
        schema_names (str): Serialized JSON array of schema names selected for export.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "tasks"
    id: str = Field(default_factory=uid)
    name: str
    target_id: str
    schema_name: str
    directory: str
    enabled: bool = True
    cron: str = "0 2 * * *"
    timezone: str = "America/Detroit"
    retention_days: int = 30
    parallel: int = 1
    file_size_gb: int = 10
    content: str = "ALL"
    consistent_snapshot: bool = False
    next_run_at: datetime | None = None
    export_mode: str = "SCHEMA"
    schema_names: str = "[]"
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ("schema_names",)
