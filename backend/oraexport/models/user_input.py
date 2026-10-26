"""UserInput Pydantic model."""

from typing import Literal

from pydantic import SecretStr, field_validator

from .login_input import LoginInput


class UserInput(LoginInput):
    """Administrator-created account.

    Attributes:
        username (str): Local account username.
        password (SecretStr): Plaintext password meeting minimum length requirements.
        role (Literal['admin', 'operator', 'viewer']): Authorized system access role.
    """

    role: Literal["admin", "operator", "viewer"] = "viewer"

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: SecretStr) -> SecretStr:
        """Require a sufficiently long application password.

        Args:
            value (SecretStr): Password secret to validate.

        Returns:
            SecretStr: Validated password meeting length requirements.

        Raises:
            ValueError: If password has fewer than 8 characters.
        """
        secret_str = value.get_secret_value()
        is_strong = len(secret_str) >= 8
        if not is_strong:
            raise ValueError("Password must have at least 8 characters")
        return value
