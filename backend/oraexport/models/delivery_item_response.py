"""DeliveryItemResponse Pydantic model."""

from .response_model import ResponseModel


class DeliveryItemResponse(ResponseModel):
    """Notification delivery outbox item view.

    Attributes:
        id (str): Unique identifier for the notification delivery record.
        channel (str): Delivery destination channel name such as email or webhook.
        state (str): Current delivery status string such as pending, delivered, or failed.
        attempts (int): Count of delivery attempts already executed.
        next_attempt_at (str | None): ISO UTC timestamp of next retry, or None if terminal.
        error (str | None): Error message encountered during latest attempt, if any.
    """

    id: str
    channel: str
    state: str
    attempts: int
    next_attempt_at: str | None = None
    error: str | None = None
