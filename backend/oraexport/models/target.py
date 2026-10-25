"""Target metadata model and Oracle table mapping."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import uid


class Target(BaseModel):
    """A directly connectable PDB or non-container database.

    Maps to the targets table. Identifies a specific database service where Data Pump
    exports are executed and where the dedicated export account is provisioned.

    Attributes:
        id (str): Unique target identifier.
        registration_id (str): Parent registration identifier.
        name (str): Target database service name.
        dsn (str): Oracle connection string for connecting directly to this target.
        admin_credential_id (str): Identifier of DBA credential for administrative tasks.
        export_credential_id (str | None): Identifier of provisioned Data Pump credential.
        directory (str | None): Oracle directory object name used for Data Pump files.
        tablespace (str): Default tablespace for the export user account.
        provisioned (bool): Flag indicating if export user and directory have been created.
        edition (str | None): Target database edition name if editioning is enabled.
        container_name (str | None): Pluggable database container name.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "targets"
    id: str = Field(default_factory=uid)
    registration_id: str
    name: str
    dsn: str
    admin_credential_id: str
    export_credential_id: str | None = None
    directory: str | None = None
    tablespace: str = "USERS"
    provisioned: bool = False
    edition: str | None = None
    container_name: str | None = None
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ()
