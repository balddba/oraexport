"""Registrations HTTP endpoints.

This module provides FastAPI routes for registering logical Oracle databases (CDB/non-CDB),
securely storing encrypted administrative DBA credentials, discovering container topology
and pluggable databases (PDBs), updating registration metadata, and removing unused registrations.
"""

from fastapi import APIRouter, Depends

from oraexport.metadata_service import MetadataService
from oraexport.models.discovery_response import DiscoveryResponse
from oraexport.models.registration_input import RegistrationInput
from oraexport.models.registration_response import RegistrationResponse
from oraexport.models.registration_update import RegistrationUpdate

from ...bootstrap import metadata_service
from ..dependencies import Admin, Db, Identity

router = APIRouter()


@router.get(path="/api/v1/registrations")
def registrations(db: Db, user: Identity) -> list[RegistrationResponse]:
    """List logical databases without credentials.

    Queries all registered Oracle database instances and returns their public
    attributes, omitting encrypted DBA credentials.

    Args:
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting registration list.

    Returns:
        list[RegistrationResponse]: List of serialized registration records.
    """
    # Fetch registered databases in display order and project onto allowed public fields
    items = MetadataService(db=db).registrations()
    reg_list = [
        RegistrationResponse(
            id=item.id,
            name=item.name,
            dsn=item.dsn,
            kind=item.kind,
            version=item.version,
        )
        for item in items
    ]
    return reg_list


@router.post(path="/api/v1/registrations", status_code=201)
def create_registration(
    body: RegistrationInput,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> RegistrationResponse:
    """Save an encrypted DBA credential and database registration.

    Validates database connection parameters, encrypts the administrative password
    using AES-GCM-256, and creates a logical database registration record.

    Args:
        body (RegistrationInput): Validated registration payload with connection parameters.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped metadata service.

    Returns:
        RegistrationResponse: Serialized registration record with public identifiers.

    Raises:
        HTTPException: If the registration name already exists or connection fails.
    """
    # Persist registration and encrypted administrative credentials transactionally
    result = service.create_registration(body=body, user=user)
    return RegistrationResponse(
        id=result.id,
        name=result.name,
        dsn=result.dsn,
        kind=result.kind,
        version=result.version,
    )


@router.post(path="/api/v1/registrations/{registration_id}/discover")
def discover_registration(
    registration_id: str,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> DiscoveryResponse:
    """Test the registration and discover container topology.

    Connects to the remote Oracle instance using the registered DBA credential,
    queries database version and CDB/PDB container topology, and returns candidate targets.

    Args:
        registration_id (str): Unique identifier of the registered database.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped metadata service.

    Returns:
        DiscoveryResponse: Discovered database topology, container list, and version information.

    Raises:
        HTTPException: If the registration does not exist or remote connection fails.
    """
    # Connect via thin driver and query V$DATABASE and V$CONTAINERS for topology
    result = service.discover_registration(registration_id=registration_id, user=user)
    return result


@router.put(path="/api/v1/registrations/{registration_id}")
def update_registration(
    registration_id: str,
    body: RegistrationUpdate,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> RegistrationResponse:
    """Update a registration and inherited DBA credentials without changing run secrets.

    Modifies connection string, name, or credentials for a registered database while
    preserving existing export run snapshot records.

    Args:
        registration_id (str): Unique identifier of the registration to update.
        body (RegistrationUpdate): Validated update payload.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped metadata service.

    Returns:
        RegistrationResponse: Serialized updated registration record.

    Raises:
        HTTPException: If the registration does not exist or updated credentials fail.
    """
    # Update registration attributes in metadata transaction
    result = service.update_registration(registration_id=registration_id, body=body, user=user)
    return RegistrationResponse(
        id=result.id,
        name=result.name,
        dsn=result.dsn,
        kind=result.kind,
        version=result.version,
    )


@router.delete(path="/api/v1/registrations/{registration_id}")
def delete_registration(
    registration_id: str,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> dict[str, bool]:
    """Remove an unused registration and its unshared encrypted credential.

    Deletes the registration record and its unreferenced administrative credentials,
    provided no export targets or export policies depend on it.

    Args:
        registration_id (str): Unique identifier of the registration to delete.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped metadata service.

    Returns:
        dict[str, bool]: Deletion confirmation dictionary.

    Raises:
        HTTPException: If dependent export targets exist or registration is not found.
    """
    # Verify no dependent export targets exist before removing registration and credential
    result = service.delete_registration(registration_id=registration_id, user=user)
    return result
