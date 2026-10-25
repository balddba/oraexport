"""TaskResponse Pydantic model."""

from pydantic import Field

from .response_model import ResponseModel


class TaskResponse(ResponseModel):
    """Export task policy public view.

    Attributes:
        id (str): Unique task policy identifier.
        target_id (str): Associated target database service identifier.
        name (str): Unique task policy name.
        schema_names (list[str]): List of application schema names to export.
        schema_name (str): Primary or legacy single schema name.
        export_mode (str): Scope of export operation (SCHEMA or FULL).
        directory (str): Oracle directory object name used for dump files.
        enabled (bool): Flag indicating whether scheduled execution is active.
        cron (str): Standard five-field cron schedule expression.
        timezone (str): IANA timezone identifier for cron schedule.
        retention_days (int): Number of days to retain completed export artifacts.
        parallel (int): Data Pump export parallel worker process count.
        file_size_gb (int): Maximum individual dump file piece size in gigabytes.
        content (str): Export content filter (ALL, DATA_ONLY, or METADATA_ONLY).
        consistent_snapshot (bool): Flag indicating whether export uses flashback snapshot.
        next_run_at (str | None): ISO 8601 formatted timestamp of next scheduled run.
    """

    id: str
    target_id: str
    name: str
    schema_names: list[str] = Field(default_factory=list)
    schema_name: str = ""
    export_mode: str = "SCHEMA"
    directory: str
    enabled: bool = True
    cron: str = "0 2 * * *"
    timezone: str = "America/Detroit"
    retention_days: int = 30
    parallel: int = 1
    file_size_gb: int = 10
    content: str = "ALL"
    consistent_snapshot: bool = False
    next_run_at: str | None = None
