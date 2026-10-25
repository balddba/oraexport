"""DiscoveryResponse Pydantic model."""

from pydantic import Field

from .discovered_pdb import DiscoveredPdb
from .response_model import ResponseModel


class DiscoveryResponse(ResponseModel):
    """Discovered Oracle container topology and pluggable databases.

    Attributes:
        kind (str): Database architecture kind (e.g. cdb or non-cdb).
        version (str | None): Oracle Database banner or version string.
        container_name (str | None): Container or instance name.
        edition (str | None): Oracle database edition.
        pdbs (list[DiscoveredPdb]): Pluggable databases discovered in the container.
    """

    kind: str
    version: str | None = None
    container_name: str | None = None
    edition: str | None = None
    pdbs: list[DiscoveredPdb] = Field(default_factory=list)
