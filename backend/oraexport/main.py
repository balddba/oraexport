"""FastAPI composition entry point for the Oracle-only export application.

This module constructs the central FastAPI application instance, configures the
startup/shutdown lifespan context, registers security middleware (origin verification,
sec-fetch-site checks, defensive browser headers), installs centralized exception handlers
mapping domain and database failures to clean HTTP error shapes, and mounts all REST routers.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from oracledb import IntegrityError

from oraexport.exceptions.application_error import ApplicationError
from oraexport.exceptions.oracle_unavailable import OracleUnavailable
from oraexport.types import CallNext

from .api.routes import auth, notifications, registrations, reports, runs, targets, tasks
from .config import get_settings
from .metadata_connections import close_metadata_pool
from .migrations import migrate_configured_database


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate settings and migrate the metadata schema before accepting requests.

    Executes startup validations to ensure required configuration variables
    (e.g. metadata database URL, encryption key) are present and valid before
    the application begins accepting client HTTP traffic. Apply metadata migrations
    before yielding; migration failures abort startup.

    Args:
        application (FastAPI): FastAPI application instance being initialized.

    Yields:
        None: Yields control during application execution.
    """
    # Force settings validation at startup; fails fast if encryption key or DB URL is invalid
    get_settings()
    try:
        migrate_configured_database()
        yield
    finally:
        close_metadata_pool()


# Construct FastAPI application with metadata and lifecycle management
app = FastAPI(title="OraExport", version="0.1.0", lifespan=lifespan)


@app.middleware(middleware_type="http")
async def browser_security(
    request: Request,
    call_next: CallNext,
) -> Response:
    """Apply same-origin mutation protection and baseline browser security headers.

    Validates that state-changing requests (POST, PUT, DELETE) originate from the
    trusted origins and rejects cross-site requests. Injects hardening security
    headers on all outbound responses.

    Args:
        request (Request): Incoming FastAPI HTTP request.
        call_next (CallNext): Next route handler.

    Returns:
        Response: Outbound HTTP response augmented with security headers.
    """
    # Enforce origin and Sec-Fetch-Site validation for state-modifying HTTP verbs
    safe_methods = {"GET", "HEAD", "OPTIONS"}
    if request.method not in safe_methods:
        origin = request.headers.get("origin")
        allowed_origins = {value.strip() for value in get_settings().allowed_origin.split(sep=",") if value.strip()}
        if origin and origin not in allowed_origins:
            error_body = {"detail": "Untrusted request origin"}
            return JSONResponse(status_code=403, content=error_body)
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site == "cross-site":
            error_body = {"detail": "Cross-site mutation rejected"}
            return JSONResponse(status_code=403, content=error_body)

    # Process request and attach standard defensive headers to response
    response: Response = await call_next(request=request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(exc_class_or_status_code=RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return structured validation errors without echoing secret input values.

    Transforms Pydantic validation errors into consistent JSON responses while
    omitting raw input values that might contain plaintext passwords or secrets.

    Args:
        request (Request): Incoming HTTP request triggering validation failure.
        exc (RequestValidationError): Exception carrying detailed validation errors.

    Returns:
        JSONResponse: Structured 422 Unprocessable Entity HTTP response.
    """
    # Format error location, message, and type without echoing sensitive input values
    error_items = []
    for error in exc.errors():
        item = {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        error_items.append(item)
    return JSONResponse(status_code=422, content={"detail": error_items})


@app.exception_handler(exc_class_or_status_code=OracleUnavailable)
async def oracle_error(request: Request, exc: OracleUnavailable) -> JSONResponse:
    """Expose only sanitized transport errors.

    Catches low-level Oracle network or database connection dropouts and returns
    a sanitized 502 Bad Gateway response without internal stack traces.

    Args:
        request (Request): Incoming HTTP request during which Oracle failed.
        exc (OracleUnavailable): Sanitized Oracle transport failure exception.

    Returns:
        JSONResponse: Sanitized 502 Bad Gateway HTTP response.
    """
    # Return sanitized database availability error
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(exc_class_or_status_code=ValueError)
async def value_error(request: Request, exc: ValueError) -> JSONResponse:
    """Return domain validation failures.

    Translates domain-level value errors (such as invalid run states or cron expressions)
    into standard 400 Bad Request responses.

    Args:
        request (Request): Incoming HTTP request triggering value error.
        exc (ValueError): Domain validation failure exception.

    Returns:
        JSONResponse: 400 Bad Request HTTP response.
    """
    # Return domain validation failure message
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(exc_class_or_status_code=IntegrityError)
async def conflict_error(request: Request, exc: IntegrityError) -> JSONResponse:
    """Hide database internals and return a conflict response.

    Catches Oracle unique constraint or foreign key violations and returns a
    clean 409 Conflict response without leaking database schema internals.

    Args:
        request (Request): Incoming HTTP request triggering database conflict.
        exc (IntegrityError): Oracle database integrity violation error.

    Returns:
        JSONResponse: 409 Conflict HTTP response.
    """
    # Conceal table and constraint names behind generic conflict message
    conflict_body = {"detail": "A record with this identity already exists"}
    return JSONResponse(status_code=409, content=conflict_body)


@app.exception_handler(exc_class_or_status_code=ApplicationError)
async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
    """Translate application failure categories at the HTTP boundary.

    Maps decoupled application-layer exceptions to their designated HTTP status codes
    and safe public error messages.

    Args:
        request (Request): Incoming HTTP request triggering application error.
        exc (ApplicationError): Application domain error carrying status code and detail.

    Returns:
        JSONResponse: HTTP response reflecting application error status and message.
    """
    # Map application error status code and detail payload to HTTP response
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Register all discrete resource routers on the application
route_modules = (auth, registrations, targets, tasks, runs, reports, notifications)
for route_module in route_modules:
    app.include_router(router=route_module.router)
