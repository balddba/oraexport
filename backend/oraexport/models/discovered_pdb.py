"""DiscoveredPdb Pydantic model."""

from pydantic import Field

from .discovered_service import DiscoveredService
from .response_model import ResponseModel


class DiscoveredPdb(ResponseModel):
    """Discovered pluggable database and associated listener services.

    Attributes:
        name (str): Pluggable database container name.
        open_mode (str): Open mode of the pluggable database (e.g. READ WRITE).
        services (list[str | DiscoveredService]): Listener services associated with the database.
    """

    name: str
    open_mode: str
    services: list[str | DiscoveredService] = Field(default_factory=list)
