"""HTTP dependencies and explicit public response serializers.

This module provides reusable FastAPI request dependencies for resolving authenticated
identities, verifying administrative roles, and projecting internal database records
into sanitized public response representations that never expose encrypted credentials.
"""

import json
from collections.abc import Callable, Generator
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from oracledb import Connection

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.metadata_connections import acquire_metadata_connection
from oraexport.metadata_service import MetadataService
from oraexport.models.backup_file_response import BackupFileResponse
from oraexport.models.credential import Credential
from oraexport.models.defaults import now
from oraexport.models.login_session import LoginSession
from oraexport.models.run import Run
from oraexport.models.run_response import RunResponse
from oraexport.models.target import Target
from oraexport.models.target_response import TargetResponse
from oraexport.models.task import Task
from oraexport.models.task_response import TaskResponse
from oraexport.models.user import User
from oraexport.models.user_response import UserResponse


def metadata_connection() -> Generator[Connection, None, None]:
    """Open a request connection and release uncommitted work explicitly.

    Yields:
        Connection: Raw Oracle connection; workflows commit successful operations.
    """
    connection = acquire_metadata_connection()
    try:
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


# Dependency type alias for acquiring the scoped metadata transaction
Db = Annotated[Connection, Depends(dependency=metadata_connection)]


def current_user(request: Request, db: Db) -> User:
    """Resolve an enabled local user from a hashed opaque session token.

    Extracts the session cookie from the HTTP request, hashes the plaintext
    token using SHA-256 to look up the active session in metadata, and validates
    that the session has not expired and the associated user account is active.

    Args:
        request (Request): Incoming FastAPI HTTP request containing client cookies.
        db (Db): Active metadata database session dependency.

    Returns:
        User: Authenticated and enabled user account record.

    Raises:
        HTTPException: If the session cookie is missing, expired, or the user
            account is disabled.
    """
    # Extract opaque session token from HTTP-only browser cookie
    token = request.cookies.get("oraexport_session")
    # Lookup hashed token digest in metadata table to prevent token exposure in database logs
    hashed_token = EncryptionSerivce.token_hash(token=token) if token else None
    if hashed_token:
        with db.cursor() as cursor:
            cursor.execute("SELECT id, user_id, expires_at FROM login_sessions WHERE id = :record_key", {"record_key": hashed_token})
            query_rows = [LoginSession(id=row[0], user_id=row[1], expires_at=row[2]) for row in cursor.fetchall()]
        session = next(iter(query_rows), None)
    else:
        session = None
    # Validate session lifespan against current UTC timestamp
    is_valid_session = session is not None and session.expires_at > now()
    user_id = session.user_id if is_valid_session else None
    if user_id:
        with db.cursor() as cursor:
            cursor.execute("SELECT id, username, password_hash, role, enabled FROM app_users WHERE id = :record_key", {"record_key": user_id})
            query_rows = [User(id=row[0], username=row[1], password_hash=row[2], role=row[3], enabled=row[4]) for row in cursor.fetchall()]
        user = next(iter(query_rows), None)
    else:
        user = None
    # Reject disabled or nonexistent accounts with 401 Unauthorized
    if not user or not user.enabled:
        raise HTTPException(status_code=401, detail="Login required")
    return user


# Dependency type alias for requiring any authenticated user
Identity = Annotated[User, Depends(dependency=current_user)]


def require_role(*roles: str) -> Callable[[User], User]:
    """Build a role dependency enforced by the API, independent of frontend controls.

    Creates a specialized dependency closure enforcing role-based access control (RBAC)
    at the API route boundary to prevent unauthorized access by lower-privileged roles.

    Args:
        *roles (str): Allowed role identifiers (e.g. 'admin', 'operator').

    Returns:
        Callable[[User], User]: FastAPI dependency callable that checks user roles.
    """

    def check(user: Identity) -> User:
        """Reject authenticated users without the required role.

        Args:
            user (Identity): Authenticated user account resolved from session.

        Returns:
            User: Verified user account when role matches the allowed set.

        Raises:
            HTTPException: If the authenticated user's role is not in the allowed list.
        """
        # Enforce strict role membership check
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return check


# Role-specific dependency aliases for endpoint protection
Admin = Annotated[User, Depends(dependency=require_role("admin"))]
Operator = Annotated[User, Depends(dependency=require_role("admin", "operator"))]

# Serialization projection field allowlists decomposed into single lines
tf_part1 = "id name target_id schema_name directory enabled cron timezone"
tf_part2 = "retention_days parallel file_size_gb content consistent_snapshot"
tf_part3 = "next_run_at export_mode"
TASK_FIELDS = f"{tf_part1} {tf_part2} {tf_part3}"
tg_part1 = "id registration_id name dsn directory"
tg_part2 = "tablespace provisioned edition container_name"
TARGET_FIELDS = f"{tg_part1} {tg_part2}"
REG_FIELDS = "id name dsn kind version"


def fields(item: object, names: str) -> dict[str, object]:
    """Serialize an explicit allowlist, excluding encrypted credentials.

    Projects an object or record onto a dictionary containing only explicitly
    named attributes, formatting datetime objects into ISO 8601 UTC strings.

    Args:
        item (object): Source record or object to serialize.
        names (str): Space-delimited list of attribute names to extract.

    Returns:
        dict[str, object]: Serialized dictionary containing allowed attributes.
    """
    result: dict[str, object] = {name: getattr(item, name) for name in names.split()}
    # Normalize datetimes to ISO 8601 UTC strings for JSON clients
    for item_entry in list(result.items()):
        key = item_entry[0]
        value = item_entry[1]
        if isinstance(value, datetime):
            result[key] = value.isoformat() + "Z"
    return result


def user_view(user: User) -> UserResponse:
    """Serialize a public user record.

    Extracts safe public attributes from a user record, excluding password
    hashes and session tokens.

    Args:
        user (User): Persisted user database record.

    Returns:
        UserResponse: Public user record with id, username, role, and enabled.
    """
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        enabled=user.enabled,
    )


def target_view(db: Connection, target: Target) -> TargetResponse:
    """Include the public export username without exposing its encrypted password.

    Merges target metadata with the associated credential's username while
    omitting all encrypted password secrets from API responses.

    Args:
        db (Connection): Active metadata database session for looking up credentials.
        target (Target): Export target record being viewed.

    Returns:
        TargetResponse: Safe target dictionary with public export username.
    """
    # Look up export credential username without decrypting or exposing secret bytes
    cred_id = target.export_credential_id
    if cred_id:
        with db.cursor() as cursor:
            cursor.execute("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key", {"record_key": cred_id})
            query_rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        credential = next(iter(query_rows), None)
    else:
        credential = None
    export_user = credential.username if credential else None
    return TargetResponse(
        id=target.id,
        registration_id=target.registration_id,
        name=target.name,
        dsn=target.dsn,
        directory=target.directory,
        tablespace=target.tablespace,
        provisioned=target.provisioned,
        edition=target.edition,
        container_name=target.container_name,
        export_username=export_user,
    )


def task_view(task: Task) -> TaskResponse:
    """Expose schema arrays while retaining the legacy single-schema API field.

    Translates stored schema names into structured lists, ensuring compatibility
    with both legacy single-schema clients and multi-schema policies.

    Args:
        task (Task): Export task record to serialize.

    Returns:
        TaskResponse: Public task representation with schema lists.
    """
    # Support both multi-schema JSON arrays and legacy single-schema fallback
    fallback_schemas = [task.schema_name] if task.export_mode == "SCHEMA" else []
    loaded_schemas = json.loads(s=task.schema_names) if task.schema_names else []
    schemas = loaded_schemas or fallback_schemas
    primary_schema = schemas[0] if schemas else ""
    next_run = task.next_run_at.isoformat() + "Z" if task.next_run_at else None
    return TaskResponse(
        id=task.id,
        target_id=task.target_id,
        name=task.name,
        schema_names=schemas,
        schema_name=primary_schema,
        export_mode=task.export_mode,
        directory=task.directory,
        enabled=task.enabled,
        cron=task.cron,
        timezone=task.timezone,
        retention_days=task.retention_days,
        parallel=task.parallel,
        file_size_gb=task.file_size_gb,
        content=task.content,
        consistent_snapshot=task.consistent_snapshot,
        next_run_at=next_run,
    )


def run_view(db: Connection, run: Run) -> RunResponse:
    """Present run history including measured dump sizes and retained file state.

    Constructs a comprehensive export execution view including filtered snapshot
    parameters, error logs, status messages, attached file manifests, aggregate
    byte sizes, and wall-clock execution duration.

    Args:
        db (Connection): Active metadata database session.
        run (Run): Export execution run record.

    Returns:
        RunResponse: Detailed run dictionary for API consumers.
    """
    snapshot_raw = json.loads(s=run.snapshot) if run.snapshot else {}
    secret_keys = {"dsn", "credential_id"}
    safe_snapshot = {k: v for k, v in snapshot_raw.items() if k not in secret_keys}
    errors = json.loads(s=run.errors) if run.errors else []
    messages = json.loads(s=run.messages) if run.messages else []
    files = MetadataService(db=db).files(run_id=run.id)
    file_responses = [
        BackupFileResponse(
            id=file.id,
            directory=file.directory,
            filename=file.filename,
            kind=file.kind,
            size_bytes=file.size_bytes,
            deleted_at=file.deleted_at.isoformat() + "Z" if file.deleted_at else None,
            cleanup_attempts=file.cleanup_attempts,
            cleanup_error=file.cleanup_error,
        )
        for file in files
    ]
    dumps = [file for file in files if file.kind == "dump"]
    all_measured = all(file.size_bytes is not None for file in dumps)
    total_size = sum(file.size_bytes for file in dumps) if dumps and all_measured else None
    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at is not None and run.started_at is not None else None

    return RunResponse(
        id=run.id,
        task_id=run.task_id,
        target_id=run.target_id,
        state=run.state,
        oracle_job=run.oracle_job,
        created_at=run.created_at.isoformat() + "Z" if run.created_at else None,
        started_at=run.started_at.isoformat() + "Z" if run.started_at else None,
        finished_at=run.finished_at.isoformat() + "Z" if run.finished_at else None,
        progress=run.progress,
        cancel_requested=run.cancel_requested,
        last_seen_at=run.last_seen_at.isoformat() + "Z" if run.last_seen_at else None,
        snapshot=safe_snapshot,
        errors=errors,
        messages=messages,
        files=file_responses,
        size_bytes=total_size,
        duration_seconds=duration,
    )
