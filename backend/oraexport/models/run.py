"""Run metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import now, uid


class Run(BaseModel):
    """Immutable requested export and its evolving execution state.

    Maps to the runs table. Retains a frozen JSON snapshot of task settings at queue time
    and tracks lifecycle state transitions from queued to succeeded or failed.

    Attributes:
        id (str): Unique run identifier.
        task_id (str): Identifier of the export task policy.
        target_id (str): Identifier of the target database service.
        occurrence (str): Scheduled or ad-hoc run occurrence timestamp string.
        snapshot (str): Serialized JSON configuration snapshot at queue time.
        state (str): Execution lifecycle state.
        oracle_job (str): Oracle Data Pump job name.
        created_at (datetime): Timestamp when the run was queued.
        started_at (datetime | None): Timestamp when execution began.
        finished_at (datetime | None): Timestamp when execution completed.
        progress (int | None): Percentage progress of Data Pump execution.
        errors (str): Serialized JSON array of execution error messages.
        messages (str): Serialized JSON array of informational execution messages.
        cancel_requested (bool): Flag indicating whether cancellation was requested.
        last_seen_at (datetime | None): Timestamp of most recent progress heartbeat.
        notification_created (bool): Flag indicating whether completion notification was generated.
        master_cleanup_pending (bool): Durable intent to remove the inactive remote master table.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "runs"
    id: str = Field(default_factory=uid)
    task_id: str
    target_id: str
    occurrence: str
    snapshot: str
    state: str = "queued"
    oracle_job: str
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: int | None = None
    errors: str = "[]"
    messages: str = "[]"
    cancel_requested: bool = False
    last_seen_at: datetime | None = None
    notification_created: bool = False
    master_cleanup_pending: bool = True
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ("snapshot", "errors", "messages")
