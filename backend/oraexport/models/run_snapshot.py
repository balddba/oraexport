"""Immutable legacy-compatible persisted run settings."""

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


def freeze(value: object) -> object:
    """Recursively detach and freeze JSON values in a run snapshot.

    Converts mutable dicts into MappingProxyType and lists into tuples,
    preventing subsequent modification by callers.

    Args:
        value (object): Value or collection to freeze recursively.

    Returns:
        object: Deeply immutable frozen value (MappingProxyType, tuple, or scalar).
    """
    if isinstance(value, dict):
        frozen_items = {key: freeze(value=item) for key, item in value.items()}
        proxy = MappingProxyType(mapping=frozen_items)
        return proxy
    if isinstance(value, list):
        frozen_elements = tuple(freeze(value=item) for item in value)
        return frozen_elements
    return value


def thaw(value: object) -> object:
    """Produce independent JSON-compatible transport values.

    Recursively converts MappingProxyType back to dict and tuples back to lists,
    producing a detached, mutable structure for serialization.

    Args:
        value (object): Frozen immutable snapshot value or collection.

    Returns:
        object: Deeply mutable JSON-compatible copy (dict, list, or scalar).
    """
    if isinstance(value, Mapping):
        thawed_items = {key: thaw(value=item) for key, item in value.items()}
        return thawed_items
    if isinstance(value, tuple):
        thawed_elements = [thaw(value=item) for item in value]
        return thawed_elements
    return value


class RunSnapshot(BaseModel):
    """Retain immutable queued settings, including older snapshot formats.

    Attributes:
        values (Mapping[str, Any]): Frozen mapping containing immutable run parameters.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: Mapping[str, Any]

    @field_validator("values")
    @classmethod
    def freeze_values(cls, values: Mapping[str, Any]) -> Mapping[str, Any]:
        """Detach and freeze nested settings after validating the mapping.

        Args:
            values (Mapping[str, Any]): Validated snapshot settings.

        Returns:
            Mapping[str, Any]: Deeply immutable copy of the settings.
        """
        return MappingProxyType(mapping={key: freeze(value=item) for key, item in values.items()})

    @field_serializer("values")
    def serialize_values(self, values: Mapping[str, Any]) -> dict:
        """Convert frozen settings into independent serializable values.

        Args:
            values (Mapping[str, Any]): Frozen snapshot settings.

        Returns:
            dict: Detached JSON-compatible settings.
        """
        return {key: thaw(value=item) for key, item in values.items()}

    @classmethod
    def from_json(cls, value: str) -> "RunSnapshot":
        """Read the persisted snapshot without rewriting legacy filenames.

        Args:
            value (str): Serialized JSON string from database CLOB column.

        Returns:
            RunSnapshot: Deserialized immutable run snapshot instance.
        """
        parsed_dict = json.loads(s=value)
        snapshot = cls(values=parsed_dict)
        return snapshot

    def to_json(self) -> str:
        """Encode settings for the existing Oracle CLOB contract.

        Returns:
            str: Serialized JSON string suitable for Oracle CLOB storage.
        """
        thawed_dict = self.options()
        serialized = json.dumps(obj=thawed_dict)
        return serialized

    def options(self) -> dict:
        """Return an independent options dictionary for remote execution.

        Returns:
            dict: Mutable, detached copy of configuration options.
        """
        mutable_opts = {key: thaw(value=item) for key, item in self.values.items()}
        return mutable_opts
