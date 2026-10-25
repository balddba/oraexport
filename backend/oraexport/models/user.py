"""User metadata model and Oracle table mapping."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import uid


class User(BaseModel):
    """Local application identity.

    Maps to the app_users table. Represents an authenticated local user account
    with an assigned role (admin or viewer) and active status.

    Attributes:
        id (str): Unique user account identifier.
        username (str): Local user login name.
        password_hash (str): Argon2 hashed user password.
        role (str): Authorized access role (admin, operator, or viewer).
        enabled (bool): Flag indicating whether the user account is active.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "app_users"
    id: str = Field(default_factory=uid)
    username: str
    password_hash: str
    role: str = "viewer"
    enabled: bool = True
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ()
