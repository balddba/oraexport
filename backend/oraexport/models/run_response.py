"""RunResponse Pydantic model."""

from pydantic import Field

from .backup_file_response import BackupFileResponse
from .response_model import ResponseModel


class RunResponse(ResponseModel):
    """Detailed export execution run view.

    Attributes:
        id (str): Unique run identifier.
        task_id (str): Identifier of the export task policy.
        target_id (str): Identifier of the target database service.
        state (str): Current execution state.
        oracle_job (str | None): Oracle Data Pump job name.
        created_at (str | None): Formatted creation timestamp string.
        started_at (str | None): Formatted execution start timestamp string.
        finished_at (str | None): Formatted execution completion timestamp string.
        progress (int | None): Percentage progress of Data Pump execution.
        cancel_requested (bool): Whether cancellation was requested.
        last_seen_at (str | None): Formatted last heartbeat timestamp string.
        snapshot (dict[str, object]): Frozen configuration snapshot dictionary.
        errors (list[str]): List of execution error messages.
        messages (list[str]): List of informational execution messages.
        files (list[BackupFileResponse]): Generated dump files and log artifacts.
        size_bytes (int | None): Total aggregate size of generated dump files in bytes.
        duration_seconds (float | None): Total execution elapsed time in seconds.
    """

    id: str
    task_id: str
    target_id: str
    state: str
    oracle_job: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    progress: int | None = None
    cancel_requested: bool = False
    last_seen_at: str | None = None
    snapshot: dict[str, object] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    files: list[BackupFileResponse] = Field(default_factory=list)
    size_bytes: int | None = None
    duration_seconds: float | None = None
