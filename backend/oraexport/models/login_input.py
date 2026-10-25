"""LoginInput Pydantic model."""

from pydantic import Field, SecretStr

from .strict_model import StrictModel


class LoginInput(StrictModel):
    """Local login credentials.

    Attributes:
        username (str): Local account username.
        password (SecretStr): Plaintext password submitted for authentication.
    """

    username: str = Field(min_length=1, max_length=100)
    password: SecretStr
