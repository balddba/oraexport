"""TargetInput Pydantic model."""

from pydantic import Field, SecretStr, field_validator

from .strict_model import StrictModel
from .validators import connection_dsn


class TargetInput(StrictModel):
    """Direct PDB/non-CDB connection under a registration.

    Attributes:
        registration_id (str): Parent database registration identifier.
        pdb_name (str | None): Pluggable database name (PDB), or None for non-CDB.
        name (str): Unique target name.
        dsn (str): Dedicated target connection string.
        username (str | None): Optional target export username.
        password (SecretStr | None): Optional target export password.
        sysdba (bool): Flag indicating SYSDBA privilege requirement.
    """

    registration_id: str
    pdb_name: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    dsn: str = Field(min_length=1, max_length=1000)
    username: str | None = None
    password: SecretStr | None = None
    sysdba: bool = False

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: str) -> str:
        """Reject connection strings containing embedded credentials."""
        return connection_dsn(value=value)
