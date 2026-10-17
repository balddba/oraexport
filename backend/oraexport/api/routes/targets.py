"""Targets HTTP endpoints.

This module provides FastAPI routes for managing connectable Oracle export targets
(pluggable databases or non-CDB instances), inspecting database catalogs (schemas,
tablespaces, and directory objects), and previewing or executing dedicated export user
and directory provisioning DDL.
"""

from fastapi import APIRouter, Depends

from oraexport.metadata_service import MetadataService
from oraexport.models.provision_input import ProvisionInput
from oraexport.models.provision_preview_response import ProvisionPreviewResponse
from oraexport.models.target_catalog_response import TargetCatalogResponse
from oraexport.models.target_input import TargetInput
from oraexport.models.target_response import TargetResponse

from ...bootstrap import metadata_service
from ...oracle_database import OracleDatabase
from ..dependencies import Admin, Db, Identity, Operator, target_view

router = APIRouter()


@router.get(path="/api/v1/targets")
def targets(db: Db, user: Identity) -> list[TargetResponse]:
    """List directly connectable export targets.

    Queries all configured export target services and returns their public metadata
    views, including associated export usernames without revealing encrypted passwords.

    Args:
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user requesting the target list.

    Returns:
        list[TargetResponse]: List of serialized target view dictionaries.
    """
    # Fetch targets in display order and project onto sanitized target view dictionaries
    target_records = MetadataService(db=db).targets()
    target_list = [target_view(db=db, target=item) for item in target_records]
    return target_list


@router.post(path="/api/v1/targets", status_code=201)
def create_target(
    body: TargetInput,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> TargetResponse:
    """Validate a PDB/non-CDB service and save its administrative credential.

    Verifies network reachability and administrative privileges against the remote
    Oracle service, encrypts credentials, and registers the connectable target.

    Args:
        body (TargetInput): Target creation payload with service DSN and credentials.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped metadata service.

    Returns:
        TargetResponse: Serialized target view dictionary for the created target.

    Raises:
        HTTPException: If connection validation fails or duplicate target name exists.
    """
    # Verify remote target connectivity and persist encrypted administrative credentials
    result = service.create_target(body=body, user=user)
    created_view = target_view(db=db, target=result)
    return created_view


@router.delete(path="/api/v1/targets/{target_id}")
def delete_target(
    target_id: str,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> dict[str, bool]:
    """Remove a target only when export policies and history do not depend on it.

    Deletes the target record and unshared export credentials, ensuring no active
    or historical export runs or configured tasks reference this target.

    Args:
        target_id (str): Unique identifier of the target to remove.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped metadata service.

    Returns:
        dict[str, bool]: Confirmation dictionary indicating successful deletion.

    Raises:
        HTTPException: If dependent export policies or execution runs still exist.
    """
    # Enforce referential integrity checks before deleting target record
    result = service.delete_target(target_id=target_id, user=user)
    return result


@router.get(path="/api/v1/targets/{target_id}/catalog")
def target_catalog(
    target_id: str,
    db: Db,
    user: Operator,
) -> TargetCatalogResponse:
    """Discover schemas and destinations using the saved DBA credential.

    Connects to the remote Oracle target database using the saved DBA credential and
    queries ALL_USERS, DBA_DIRECTORIES, and DBA_TABLESPACES to enumerate candidates.

    Args:
        target_id (str): Unique identifier of the target database.
        db (Db): Active metadata database session dependency.
        user (Operator): Authenticated operator caller enforcing RBAC.

    Returns:
        TargetCatalogResponse: Discovered catalog with schemas, directories, and tablespaces.

    Raises:
        HTTPException: If the target does not exist or remote connection fails.
    """
    # Discover database objects through remote metadata queries
    catalog_data = OracleDatabase(target_id=target_id, session=db).catalog(user=user)
    return catalog_data


@router.post(path="/api/v1/targets/{target_id}/provision/preview")
def provision_preview(
    target_id: str,
    body: ProvisionInput,
    db: Db,
    user: Admin,
) -> ProvisionPreviewResponse:
    """Generate the SQL that administrators review before provisioning.

    Renders idempotent, parameterized Oracle DDL statements (CREATE USER, GRANT
    DATAPUMP_EXP_FULL_DATABASE, CREATE OR REPLACE DIRECTORY) without executing them.

    Args:
        target_id (str): Unique identifier of the target database.
        body (ProvisionInput): Provisioning options including username, tablespace, directory.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.

    Returns:
        ProvisionPreviewResponse: Formatted SQL script and review notes.

    Raises:
        HTTPException: If target options are invalid or target does not exist.
    """
    # Generate reviewable SQL script without altering remote database state
    preview_data = OracleDatabase(target_id=target_id, session=db).provision_preview(body=body, user=user)
    return preview_data


@router.post(path="/api/v1/targets/{target_id}/provision/apply")
def provision_apply(
    target_id: str,
    body: ProvisionInput,
    db: Db,
    user: Admin,
) -> TargetResponse:
    """Apply the reviewed provisioning definition and verify directory access.

    Executes the reviewed DDL statements on the remote target, verifies UTL_FILE
    directory write and delete capabilities, and saves the dedicated export credential.

    Args:
        target_id (str): Unique identifier of the target database.
        body (ProvisionInput): Validated provisioning input payload.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.

    Returns:
        TargetResponse: Updated target view dictionary reflecting provisioned status.

    Raises:
        HTTPException: If remote execution fails or directory verification fails.
    """
    # Execute reviewed DDL on remote database, probe directory permissions, and update target
    result = OracleDatabase(target_id=target_id, session=db).provision_apply(body=body, user=user)
    updated_view = target_view(db=db, target=result)
    return updated_view
