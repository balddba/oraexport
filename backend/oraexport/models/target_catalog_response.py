"""TargetCatalogResponse Pydantic model."""

from pydantic import Field

from .directory_info import DirectoryInfo
from .discovered_pdb import DiscoveredPdb
from .response_model import ResponseModel


class TargetCatalogResponse(ResponseModel):
    """Discovered target catalog containing schemas, directories, and tablespaces.

    Attributes:
        kind (str): Database architecture kind (CDB or NON_CDB).
        container_name (str): Container or root database service name.
        version (str): Oracle Database software release version.
        edition (str): Oracle Database edition (Enterprise or Standard).
        pdbs (list[DiscoveredPdb]): List of pluggable databases discovered in the container.
        schemas (list[str]): Discovered application schema names available for export.
        directories (list[DirectoryInfo]): Oracle directory objects accessible for dump files.
        tablespaces (list[str]): Permanent tablespaces available for provisioning or storage.
    """

    kind: str
    container_name: str
    version: str
    edition: str
    pdbs: list[DiscoveredPdb] = Field(default_factory=list)
    schemas: list[str] = Field(default_factory=list)
    directories: list[DirectoryInfo] = Field(default_factory=list)
    tablespaces: list[str] = Field(default_factory=list)
