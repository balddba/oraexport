"""Validated scalar values for CSV report cells."""

from datetime import datetime

from pydantic import ConfigDict, RootModel


class CellScalar(RootModel[str | int | float | bool | datetime | None]):
    """Retain a CSV scalar without coercing strings, numbers, or timestamps."""

    model_config = ConfigDict(strict=True)
