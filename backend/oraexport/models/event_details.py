"""Validated notification event details."""

from pydantic import JsonValue, RootModel


class EventDetails(RootModel[dict[str, JsonValue]]):
    """Retain a JSON object of event-specific notification details."""
