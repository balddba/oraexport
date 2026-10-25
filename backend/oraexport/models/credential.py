"""Credential metadata model and Oracle table mapping."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import uid


class Credential(BaseModel):
    """Encrypted target database credentials.

    Maps to the credentials table. Stores usernames and Fernet-encrypted ciphertext
    passwords for DBA and Data Pump connections.

    Attributes:
        id (str): Unique identifier for the credential record.
        username (str): Database user or account name.
        ciphertext (str): Fernet-encrypted password ciphertext string.
        sysdba (bool): Flag indicating if connection requires SYSDBA privilege.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "credentials"
    id: str = Field(default_factory=uid)
    username: str
    ciphertext: str
    sysdba: bool = False
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ("ciphertext",)
