"""LoginSession metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class LoginSession(BaseModel):
    """Hashed opaque browser session token.

    Maps to the login_sessions table. Stores session expiration timestamps indexed
    by the SHA-256 hash of the browser session cookie token.

    Attributes:
        id (str): SHA-256 hash of the session cookie token.
        user_id (str): Identifier of the authenticated user.
        expires_at (datetime): UTC timestamp when the session expires.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "login_sessions"
    id: str
    user_id: str
    expires_at: datetime
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ()
