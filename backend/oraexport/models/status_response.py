"""StatusResponse Pydantic model."""

from .response_model import ResponseModel


class StatusResponse(ResponseModel):
    """Generic status indicator response.

    Attributes:
        ok (bool): Boolean status indicating success or failure.
    """

    ok: bool = True
