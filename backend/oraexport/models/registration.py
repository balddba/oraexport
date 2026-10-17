"""Registration metadata model and Oracle table mapping."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import uid


class Registration(BaseModel):
    """Logical database registration, optionally a CDB root.

    Maps to the registrations table. Represents an Oracle listener endpoint
    and DBA connection used for container and database discovery.

    Attributes:
        id (str): Unique identifier for the database registration.
        name (str): Unique logical display name for the database instance.
        dsn (str): Oracle Net connection string or Easy Connect DSN.
        credential_id (str): Foreign key to the stored DBA credential.
        kind (str): Database deployment kind (e.g., cdb, non-cdb).
        version (str | None): Oracle Database banner version string.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "registrations"
    id: str = Field(default_factory=uid)
    name: str
    dsn: str
    credential_id: str
    kind: str = "unknown"
    version: str | None = None
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ()
