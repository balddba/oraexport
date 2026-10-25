"""Job status model for export execution."""

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(BaseModel):
    """Portable Data Pump status independent of Oracle object types.

    Attributes:
        state (str): Remote Oracle job state string (e.g., 'EXECUTING', 'COMPLETED').
        progress (int | None): Percentage completion (0-100) or None if unknown.
        error_count (int): Count of recorded database or utility errors.
        errors (list[str]): List of captured error messages (ORA-*, UDE-*).
        messages (list[str]): Status and diagnostic log messages from Data Pump.
        files (list[str]): Produced dump pieces and log files owned by the run.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    state: str
    progress: int | None = None
    error_count: int = 0
    errors: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
