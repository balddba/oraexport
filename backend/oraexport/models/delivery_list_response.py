"""DeliveryListResponse Pydantic model."""

from .delivery_item_response import DeliveryItemResponse
from .response_model import ResponseModel


class DeliveryListResponse(ResponseModel):
    """Paginated notification outbox delivery listing.

    Attributes:
        items (list[DeliveryItemResponse]): List of notification delivery records for current page.
        total (int): Total number of notification deliveries matching the query.
    """

    items: list[DeliveryItemResponse]
    total: int
