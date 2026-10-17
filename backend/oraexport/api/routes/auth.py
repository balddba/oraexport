"""Auth HTTP endpoints.

This module provides FastAPI routes for metadata health checks, user authentication,
opaque session cookie management, current user identity inspection, and administrative
local user account lifecycle management (creation, listing, disablement).
"""

from fastapi import APIRouter, Depends, Request, Response

from oraexport.metadata_service import MetadataService
from oraexport.models.login_input import LoginInput
from oraexport.models.user_input import UserInput
from oraexport.models.user_response import UserResponse

from ...bootstrap import metadata_service
from ...config import get_settings
from ..dependencies import Admin, Db, Identity, user_view

router = APIRouter()


@router.get(path="/api/v1/health")
def health(db: Db) -> dict[str, str]:
    """Check metadata connectivity without exposing configuration.

    Executes a trivial query against Oracle dual table to confirm metadata
    database connectivity and responsiveness.

    Args:
        db (Db): Active metadata database session dependency.

    Returns:
        dict[str, str]: Status dictionary indicating health status.
    """
    # Execute lightweight probe query to verify Oracle metadata session liveness
    MetadataService(db=db).health()
    health_status = {"status": "ok"}
    return health_status


@router.post(path="/api/v1/auth/login")
def login(
    body: LoginInput,
    response: Response,
    db: Db,
    service: MetadataService = Depends(dependency=metadata_service),
) -> UserResponse:
    """Authenticate and issue a secure opaque browser cookie.

    Verifies user credentials against stored Argon2id hashes, creates a persisted
    opaque session token in metadata, and attaches a secure HTTP-only session cookie
    to the HTTP response.

    Args:
        body (LoginInput): Login request payload with username and secret password.
        response (Response): FastAPI HTTP response object for setting the session cookie.
        db (Db): Active metadata database session dependency.
        service (MetadataService): Request-scoped account and metadata workflows.

    Returns:
        UserResponse: Serialized public user profile for the authenticated account.

    Raises:
        HTTPException: If credentials are invalid or the account is disabled.
    """
    # Decompose login result tuple into single-line variable assignments
    secret_password = body.password.get_secret_value()
    login_result = service.login(username=body.username, password=secret_password)
    user = login_result[0]
    token = login_result[1]
    # Configure security parameters for the browser session cookie
    settings = get_settings()
    cookie_lifetime = settings.session_hours * 3600
    is_secure = settings.cookie_secure
    # Set HTTP-only cookie with strict SameSite policy to prevent XSS and CSRF token leakage
    response.set_cookie(
        key="oraexport_session",
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="strict",
        max_age=cookie_lifetime,
        path="/api/v1",
    )
    user_data = user_view(user=user)
    return user_data


@router.get(path="/api/v1/auth/me")
def me(user: Identity) -> UserResponse:
    """Return the current user's role and identity.

    Inspects the authenticated identity resolved by the session dependency
    and returns its safe public profile representation.

    Args:
        user (Identity): Authenticated user account resolved from session.

    Returns:
        UserResponse: Serialized public user profile with role and identity.
    """
    # Project user record into public schema excluding password hashes
    user_data = user_view(user=user)
    return user_data


@router.post(path="/api/v1/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: Db,
    user: Identity,
    service: MetadataService = Depends(dependency=metadata_service),
) -> dict[str, bool]:
    """Invalidate the current session and clear its cookie.

    Deletes the active session from metadata storage and instructs the client
    browser to delete the session cookie.

    Args:
        request (Request): Incoming FastAPI HTTP request carrying session cookie.
        response (Response): FastAPI HTTP response for clearing the cookie.
        db (Db): Active metadata database session dependency.
        user (Identity): Authenticated user performing the logout.
        service (MetadataService): Request-scoped account and metadata workflows.

    Returns:
        dict[str, bool]: Acknowledgement dictionary confirming logout.
    """
    # Extract session cookie value to invalidate from metadata
    token = request.cookies["oraexport_session"]
    service.logout(token=token)
    # Clear the session cookie on the client browser
    response.delete_cookie(key="oraexport_session", path="/api/v1")
    logout_status = {"ok": True}
    return logout_status


@router.get(path="/api/v1/users")
def users(db: Db, user: Admin) -> list[UserResponse]:
    """List accounts for administrators.

    Queries all configured user accounts in username order for administrative
    audit and account management.

    Args:
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.

    Returns:
        list[UserResponse]: List of serialized public user profiles.
    """
    # Query user records ordered by username through the repository
    records = MetadataService(db=db).users()
    user_list = [user_view(user=item) for item in records]
    return user_list


@router.post(path="/api/v1/users", status_code=201)
def create_user(
    body: UserInput,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> UserResponse:
    """Create a local account with an explicit role.

    Creates a new user record with a hashed password, assigns an explicit role,
    and records an administrative audit entry in an atomic transaction.

    Args:
        body (UserInput): Validated user creation payload with username, password, and role.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped account and metadata workflows.

    Returns:
        UserResponse: Serialized public user profile for the created account.

    Raises:
        HTTPException: If the username already exists or password is too short.
    """
    # Create the user and audit record in the metadata transaction
    result = service.create_user(body=body, user=user)
    created_profile = user_view(user=result)
    return created_profile


@router.post(path="/api/v1/users/{user_id}/disable")
def disable_user(
    user_id: str,
    db: Db,
    user: Admin,
    service: MetadataService = Depends(dependency=metadata_service),
) -> UserResponse:
    """Disable another account and invalidate its effective access.

    Sets the user's enabled flag to False and invalidates any active sessions,
    preventing subsequent authentication and authorization checks.

    Args:
        user_id (str): Unique identifier of the user account to disable.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (MetadataService): Request-scoped account and metadata workflows.

    Returns:
        UserResponse: Serialized public user profile for the disabled account.

    Raises:
        HTTPException: If an administrator attempts to disable their own account.
    """
    # Invalidate account and revoke all outstanding sessions
    result = service.disable_user(user_id=user_id, user=user)
    disabled_profile = user_view(user=result)
    return disabled_profile
