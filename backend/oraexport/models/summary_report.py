"""SummaryReport Pydantic model."""

from pydantic import Field

from .response_model import ResponseModel


class SummaryReport(ResponseModel):
    """Operational summary metrics across runs and storage.

    Attributes:
        counts (dict[str, int]): Run counts grouped by state.
        retained_bytes (int): Total storage bytes retained by export files.
        tasks (int): Total number of configured export tasks.
        targets (int): Total number of registered export targets.
    """

    counts: dict[str, int] = Field(default_factory=dict)
    retained_bytes: int = 0
    tasks: int = 0
    targets: int = 0
