"""NotificationSettings metadata model and Oracle table mapping."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class NotificationSettings(BaseModel):
    """Singleton email/webhook configuration with encrypted secrets.

    Maps to the notification_settings table. Stores JSON configuration dictionaries
    for email and webhook notification transports alongside encrypted credentials.

    Attributes:
        id (int): Primary key identifier for the singleton settings record.
        config (str): JSON-serialized configuration dictionary for notification transports.
        secrets (str | None): Encrypted credentials payload or None if not configured.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "notification_settings"
    id: int = 1
    config: str = "{}"
    secrets: str | None = None
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ("config", "secrets")
