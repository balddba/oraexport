"""ReconcileInput Pydantic model."""

from typing import Literal

from pydantic import Field

from .strict_model import StrictModel


class ReconcileInput(StrictModel):
    """Admin acknowledgment for a stranded job whose inactivity is verified remotely.

    Attributes:
        outcome (Literal['failed', 'cancelled']): Terminal state to assign to stranded run.
        reason (str): Mandatory audit justification (10-1000 characters).
    """

    outcome: Literal["failed", "cancelled"] = "failed"
    reason: str = Field(min_length=10, max_length=1000)
