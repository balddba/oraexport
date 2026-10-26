"""Validated credentials for a self-service password change."""

from pydantic import Field, SecretStr

from .strict_model import StrictModel


class PasswordChangeInput(StrictModel):
    """Current password and a replacement meeting the account password policy."""

    current_password: SecretStr
    new_password: SecretStr = Field(min_length=8)
