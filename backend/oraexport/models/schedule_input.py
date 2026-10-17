"""ScheduleInput Pydantic model."""

from pydantic import field_validator

from .strict_model import StrictModel
from .task_input import TaskInput


class ScheduleInput(StrictModel):
    """Schedule preview request.

    Attributes:
        cron (str): Five-field cron expression string.
        timezone (str): IANA timezone identifier string.
    """

    cron: str
    timezone: str

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """Validate the schedule using the shared task rules."""
        return TaskInput.valid_cron(value=value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Validate the timezone using the shared task rules."""
        return TaskInput.valid_timezone(value=value)
