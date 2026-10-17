"""TaskInput Pydantic model."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import Field, field_validator, model_validator

from .strict_model import StrictModel
from .validators import identifier


class TaskInput(StrictModel):
    """A validated schema selection or full-target export policy.

    Attributes:
        name (str): Unique task policy name.
        target_id (str): Identifier of target database service.
        schema_name (str): Legacy single-schema name.
        schema_names (list[str]): List of application schema names to export.
        export_mode (Literal['SCHEMA', 'FULL']): Scope of export operation.
        directory (str): Oracle directory object name.
        enabled (bool): Whether automatic scheduled execution is active.
        cron (str): Standard five-field cron schedule expression.
        timezone (str): IANA timezone identifier for cron schedule.
        retention_days (int): Number of days to retain completed export artifacts.
        parallel (int): Data Pump PARALLEL worker process degree.
        file_size_gb (int): Maximum individual dump file piece size in gigabytes.
        content (Literal['ALL', 'DATA_ONLY', 'METADATA_ONLY']): Export content filter.
        consistent_snapshot (bool): Whether to execute export with FLASHBACK_TIME.
    """

    name: str = Field(min_length=1, max_length=100)
    target_id: str
    schema_name: str = ""
    schema_names: list[str] = Field(default_factory=list, max_length=200)
    export_mode: Literal["SCHEMA", "FULL"] = "SCHEMA"
    directory: str
    enabled: bool = True
    cron: str = "0 2 * * *"
    timezone: str = "America/Detroit"
    retention_days: int = Field(default=30, ge=1, le=36500)
    parallel: int = Field(default=1, ge=1, le=64)
    file_size_gb: int = Field(default=10, ge=1, le=16384)
    content: Literal["ALL", "DATA_ONLY", "METADATA_ONLY"] = "ALL"
    consistent_snapshot: bool = False

    @field_validator("directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        """Validate and normalize the Oracle directory name."""
        return identifier(value=value)

    @model_validator(mode="after")
    def export_scope(self) -> "TaskInput":
        """Normalize legacy selections and reject empty or ambiguous export scopes.

        Returns:
            TaskInput: Validated task input with normalized schema lists.

        Raises:
            ValueError: If scope is invalid, empty, or targets system schemas.
        """
        # Invariant: Full exports cannot specify schemas.
        if self.export_mode == "FULL":
            if self.schema_names or self.schema_name:
                raise ValueError("Full exports cannot specify a schema selection")
            return self

        # Normalize schema list from schema_names array or fallback schema_name.
        fallback_schema = [self.schema_name] if self.schema_name else []
        selected = self.schema_names or fallback_schema
        if not selected:
            raise ValueError("Select at least one application schema")

        # Uppercase and deduplicate schema identifiers.
        id_list = [identifier(value=name) for name in selected]
        normalized = list(dict.fromkeys(id_list))

        # Invariant: System administrative schemas cannot be exported as application schemas.
        forbidden = {"SYS", "SYSTEM", "PDBADMIN"}
        has_forbidden = any(name in forbidden for name in normalized)
        if has_forbidden:
            raise ValueError("Select application schemas")

        # Invariant: Legacy single schema field must match the primary schema when both present.
        if self.schema_names and self.schema_name:
            norm_legacy = identifier(value=self.schema_name)
            if norm_legacy != normalized[0]:
                raise ValueError("The legacy schema name must match the first selected schema")

        self.schema_names = normalized
        self.schema_name = normalized[0]
        return self

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        """Accept standard five-field cron only.

        Args:
            value (str): Cron schedule expression string.

        Returns:
            str: Validated 5-field cron expression.

        Raises:
            ValueError: If expression does not have exactly 5 fields or is invalid.
        """
        fields = value.split()
        is_five_fields = len(fields) == 5
        is_valid_expr = croniter.is_valid(expression=value)
        if not (is_five_fields and is_valid_expr):
            raise ValueError("Supply a valid five-field cron expression")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """Validate the IANA timezone.

        Args:
            value (str): IANA timezone identifier string.

        Returns:
            str: Validated timezone name.

        Raises:
            ValueError: If timezone name is not recognized by ZoneInfo.
        """
        try:
            ZoneInfo(key=value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value
