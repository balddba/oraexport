"""Validated tuple-shaped database rows."""

from pydantic import ConfigDict, RootModel


class RowTuple(RootModel[tuple[object, ...]]):
    """Retain an arbitrary database row without coercing its cell values."""

    model_config = ConfigDict(strict=True)
