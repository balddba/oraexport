"""RegistrationInput Pydantic model."""

from pydantic import Field, SecretStr, field_validator

from .strict_model import StrictModel
from .validators import connection_dsn


class RegistrationInput(StrictModel):
    """Saved database and DBA connection.

    Attributes:
        name (str): Unique logical display name for the database instance.
        dsn (str): Oracle Net connection string or Easy Connect DSN.
        username (str): Administrative database username (e.g., SYSTEM).
        password (SecretStr): Administrative database password.
        sysdba (bool): Flag indicating whether connection requires SYSDBA privilege.
    """

    name: str = Field(min_length=1, max_length=100)
    dsn: str = Field(min_length=1, max_length=1000)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr
    sysdba: bool = False

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: str) -> str:
        """Reject connection strings containing embedded credentials."""
        return connection_dsn(value=value)
