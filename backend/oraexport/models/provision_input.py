"""ProvisionInput Pydantic model."""

from pydantic import Field, field_validator

from .strict_model import StrictModel
from .validators import identifier


class ProvisionInput(StrictModel):
    """Reviewed provisioning options; passwords are generated server-side.

    Attributes:
        export_username (str): Oracle user to create or configure for export tasks.
        directory (str): Oracle DIRECTORY object where dumps will be written.
        tablespace (str): Default tablespace for the export user.
        quota_mb (int): Tablespace quota limit in megabytes.
        consistent_snapshot (bool): Whether to grant FLASHBACK privileges for flashback SCN.
    """

    export_username: str = "ORAEXPORT"
    directory: str
    tablespace: str = "USERS"
    quota_mb: int = Field(default=100, ge=10, le=102400)
    consistent_snapshot: bool = False

    @field_validator("export_username", "directory", "tablespace")
    @classmethod
    def validate_names(cls, value: str) -> str:
        """Validate and normalize Oracle object names."""
        return identifier(value=value)
