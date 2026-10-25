"""Export run model for export execution."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from oraexport.models.job_status import JobStatus
from oraexport.models.run_state import TERMINAL


class ExportRun(BaseModel):
    """Own execution transitions and accumulated observations for a run.

    Attributes:
        state (str): Current execution state string.
        cancel_requested (bool): Flag indicating if cancellation was requested.
        progress (int | None): Observed percentage progress (0-100).
        errors (list[str]): Deduplicated error messages, bounded at 500 items.
        messages (list[str]): Diagnostic messages, bounded at 500 items.
        last_seen_at (datetime | None): Timestamp of latest observation.
        finished_at (datetime | None): Timestamp when run entered terminal state.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    state: str
    cancel_requested: bool = False
    progress: int | None = None
    errors: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    finished_at: datetime | None = None

    def classify(self, status: JobStatus) -> str:
        """Determine outcomes from explicit remote state, never progress alone.

        Invariants:
        1. Completion status takes priority over progress percentage.
        2. A completed job with errors is classified as 'completed_with_errors'.
        3. Stopped jobs are classified as 'cancelled' if cancellation was requested,
           or 'failed' if stopped unexpectedly.

        Args:
            status (JobStatus): Remote job status observed from Oracle Data Pump views or logs.

        Returns:
            str: Internal RunState string corresponding to the observed status.
        """
        if status.state == "COMPLETED":
            has_errors = bool(status.error_count or status.errors)
            return "completed_with_errors" if has_errors else "succeeded"

        if status.state in {"STOPPED", "NOT RUNNING"}:
            return "cancelled" if self.cancel_requested else "failed"

        # Active states represent healthy in-flight jobs.
        active_states = {"EXECUTING", "COMPLETING", "IDLING", "STOP PENDING", "STOPPING"}
        is_active = status.state in active_states
        return "running" if is_active else "reconciliation_required"

    def apply_status(self, status: JobStatus, observed_at: datetime) -> None:
        """Apply an observation without erasing earlier export errors.

        Invariants:
        1. Monotonic error escalation: A run that previously encountered errors
           cannot be downgraded to pristine 'succeeded'.
        2. Errors and messages are deduplicated and bounded to prevent unbound growth.
        3. Terminal transitions record the finishing timestamp.

        Args:
            status (JobStatus): Newly observed Data Pump job status.
            observed_at (datetime): UTC timestamp when observation occurred.
        """
        previous_with_errors = self.state == "completed_with_errors"
        self.state = self.classify(status=status)

        # Monotonic invariant: retain completed_with_errors if any errors ever occurred.
        has_prior_errors = bool(self.errors or previous_with_errors)
        if self.state == "succeeded" and has_prior_errors:
            self.state = "completed_with_errors"

        self.progress = status.progress
        self.last_seen_at = observed_at

        # Deduplicate and bound error messages to the most recent 500 entries.
        combined_errors = self.errors + status.errors
        deduped_errors = list(dict.fromkeys(combined_errors))
        self.errors = deduped_errors[-500:]

        # Deduplicate and bound status messages to the most recent 500 entries.
        combined_messages = self.messages + status.messages
        deduped_messages = list(dict.fromkeys(combined_messages))
        self.messages = deduped_messages[-500:]

        if self.state in TERMINAL:
            self.finished_at = observed_at

    def request_cancel(self, requested_at: datetime) -> None:
        """Cancel queued work immediately and request remote cancellation otherwise.

        Args:
            requested_at (datetime): UTC timestamp when cancellation was requested.

        Raises:
            ValueError: If the run already has a confirmed terminal outcome.
        """
        if self.state in TERMINAL:
            raise ValueError("Run is already terminal")

        self.cancel_requested = True
        # Queued runs have not started remotely, so cancel them immediately.
        if self.state == "queued":
            self.state = "cancelled"
            self.finished_at = requested_at

    def acknowledge(self, outcome: str, reason: str, acknowledged_at: datetime) -> None:
        """Resolve an uncertain run after the application verifies remote inactivity.

        Invariants:
        1. Only runs in 'reconciliation_required' may be acknowledged.
        2. Operators can acknowledge as 'failed' or 'cancelled', never 'succeeded'.
        3. A non-empty reason is mandatory for audit accountability.

        Args:
            outcome (str): Target state, must be 'failed' or 'cancelled'.
            reason (str): Non-empty explanation for admin acknowledgment.
            acknowledged_at (datetime): UTC timestamp of acknowledgment.

        Raises:
            ValueError: If the run, requested outcome, or explanation is invalid.
        """
        if self.state != "reconciliation_required":
            raise ValueError("Only stranded runs can be acknowledged")

        valid_outcomes = {"failed", "cancelled"}
        is_valid_outcome = outcome in valid_outcomes
        stripped_reason = reason.strip()
        has_reason = bool(stripped_reason)
        if not (is_valid_outcome and has_reason):
            raise ValueError("Acknowledgment requires failure/cancellation and a reason")

        self.state = outcome
        self.finished_at = acknowledged_at
        audit_entry = "Admin acknowledgment: " + stripped_reason
        self.messages.append(audit_entry)
