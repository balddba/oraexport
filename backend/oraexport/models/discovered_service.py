"""DiscoveredService Pydantic model."""

from .response_model import ResponseModel


class DiscoveredService(ResponseModel):
    """Discovered database network service.

    Attributes:
        name (str): Service identifier or network alias.
        service_name (str | None): Oracle listener service name.
        dsn (str | None): Data source name connection string for the service.
    """

    name: str = ""
    service_name: str | None = None
    dsn: str | None = None
