"""Delivery metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import now, uid


class Delivery(BaseModel):
    """Durable bounded-retry notification outbox.

    Maps to the notification_deliveries table. Persists transactional notification
    events for reliable asynchronous delivery across email and webhook channels.

    Attributes:
        id (str): Unique identifier for the notification delivery record.
        channel (str): Delivery destination channel name such as email or webhook.
        payload (str): Serialized JSON payload containing notification details.
        state (str): Current delivery status string such as pending, delivered, or failed.
        attempts (int): Count of delivery attempts already executed.
        next_attempt_at (datetime): UTC timestamp when next retry attempt should occur.
        error (str | None): Error message encountered during latest attempt, if any.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "notification_deliveries"
    id: str = Field(default_factory=uid)
    channel: str
    payload: str
    state: str = "pending"
    attempts: int = 0
    next_attempt_at: datetime = Field(default_factory=now)
    error: str | None = None
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ("payload", "error")
