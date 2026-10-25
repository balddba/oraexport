"""RegistrationUpdate Pydantic model."""

from pydantic import Field, SecretStr, field_validator, model_validator

from .strict_model import StrictModel
from .validators import connection_dsn


class RegistrationUpdate(StrictModel):
    """Edit connection details; omitted credentials preserve the saved secret.

    Attributes:
        name (str): Updated display name.
        dsn (str): Updated connection string.
        username (str | None): Optional replacement username.
        password (SecretStr | None): Optional replacement password.
        sysdba (bool | None): Optional replacement SYSDBA mode flag.
    """

    name: str = Field(min_length=1, max_length=100)
    dsn: str = Field(min_length=1, max_length=1000)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: SecretStr | None = None
    sysdba: bool | None = None

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: str) -> str:
        """Reject connection strings containing embedded credentials."""
        return connection_dsn(value=value)

    @model_validator(mode="after")
    def complete_credentials(self) -> "RegistrationUpdate":
        """Require a complete, nonempty credential replacement when supplied.

        Returns:
            RegistrationUpdate: Validated instance with complete credentials.

        Raises:
            ValueError: If credential fields are partially provided.
        """
        cred_keys = {"username", "password", "sysdba"}
        intersected = cred_keys & self.model_fields_set
        if intersected:
            has_user = bool(self.username)
            has_pwd = self.password is not None and bool(self.password.get_secret_value())
            has_sysdba = self.sysdba is not None
            if not (has_user and has_pwd and has_sysdba):
                raise ValueError("Supply username, password, and SYSDBA mode together")
        return self
