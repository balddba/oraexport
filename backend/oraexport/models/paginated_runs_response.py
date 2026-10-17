"""PaginatedRunsResponse Pydantic model."""

from .response_model import ResponseModel
from .run_response import RunResponse


class PaginatedRunsResponse(ResponseModel):
    """Paginated export run execution history.

    Attributes:
        items (list[RunResponse]): List of export run records for the current page.
        total (int): Total number of run records across all pages.
        page (int): Current 1-based page number.
        page_size (int): Maximum number of run records per page.
    """

    items: list[RunResponse]
    total: int
    page: int
    page_size: int
