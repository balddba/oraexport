"""RegistrationResponse Pydantic model."""

from .response_model import ResponseModel


class RegistrationResponse(ResponseModel):
    """Database registration public view.

    Attributes:
        id (str): Unique identifier for the database registration.
        name (str): Unique logical display name for the database instance.
        dsn (str): Oracle Net connection string or Easy Connect DSN.
        kind (str | None): Database deployment kind (e.g., cdb, non-cdb).
        version (str | None): Oracle Database banner version string.
    """

    id: str
    name: str
    dsn: str
    kind: str | None = None
    version: str | None = None
