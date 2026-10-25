"""UserResponse Pydantic model."""

from .response_model import ResponseModel


class UserResponse(ResponseModel):
    """Public user account view.

    Attributes:
        id (str): Unique user account identifier.
        username (str): Local user login name.
        role (str): Assigned authorization role (admin, operator, or viewer).
        enabled (bool): Flag indicating whether the account is enabled.
    """

    id: str
    username: str
    role: str
    enabled: bool
