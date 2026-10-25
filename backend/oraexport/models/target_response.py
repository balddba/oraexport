"""TargetResponse Pydantic model."""

from .response_model import ResponseModel


class TargetResponse(ResponseModel):
    """Target database service public view.

    Attributes:
        id (str): Unique target database identifier.
        registration_id (str): Parent database registration identifier.
        name (str): Target database service name.
        dsn (str): Oracle connection data source name string.
        directory (str | None): Default Oracle directory object for export dump files.
        tablespace (str | None): Default tablespace for export auxiliary objects.
        provisioned (bool): Whether dedicated export credentials have been provisioned.
        edition (str | None): Oracle Database edition (Enterprise or Standard).
        container_name (str | None): Container or pluggable database name.
        export_username (str | None): Provisioned export user account name.
    """

    id: str
    registration_id: str
    name: str
    dsn: str
    directory: str | None = None
    tablespace: str | None = None
    provisioned: bool = False
    edition: str | None = None
    container_name: str | None = None
    export_username: str | None = None
