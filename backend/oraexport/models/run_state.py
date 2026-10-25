"""Execution state rules independent of metadata storage and Oracle transport."""

from enum import StrEnum


class RunState(StrEnum):
    """Persisted export outcomes and execution stages.

    Attributes:
        QUEUED: Export task queued in metadata, awaiting worker acquisition.
        STARTING: Worker has acquired the task and is initializing Data Pump job.
        RUNNING: Data Pump job is actively executing on the remote Oracle target.
        RECONCILIATION_REQUIRED: Worker lost connection or status is ambiguous.
        SUCCEEDED: Export finished cleanly with zero errors.
        COMPLETED_WITH_ERRORS: Export finished with non-fatal ORA-/UDE- warnings.
        FAILED: Export aborted or stopped due to fatal database error.
        CANCELLED: Operator requested cancellation and run halted.
    """

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUCCEEDED = "succeeded"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Active runs in intermediate execution states requiring worker monitoring.
ACTIVE = {RunState.STARTING, RunState.RUNNING, RunState.RECONCILIATION_REQUIRED}

# Busy runs consuming concurrency slots (active plus pending queued work).
BUSY = ACTIVE | {RunState.QUEUED}

# Terminal states representing completed executions eligible for retention cleanup.
TERMINAL = {RunState.SUCCEEDED, RunState.COMPLETED_WITH_ERRORS, RunState.FAILED, RunState.CANCELLED}
