"""NotificationSettingsResponse Pydantic model."""

from pydantic import Field

from .response_model import ResponseModel


class NotificationSettingsResponse(ResponseModel):
    """Sanitized notification channel configuration.

    Attributes:
        smtp_host (str | None): SMTP server hostname or IP address.
        smtp_port (int): SMTP server port number.
        smtp_starttls (bool): Whether to use STARTTLS for SMTP connections.
        smtp_username (str | None): Username for SMTP authentication.
        email_from (str | None): Sender email address for notifications.
        email_to (list[str]): Recipient email addresses for notifications.
        webhook_url (str | None): Target URL for webhook notifications.
        has_smtp_password (bool): Flag indicating whether an SMTP password is configured.
        has_webhook_token (bool): Flag indicating whether a webhook bearer token is configured.
    """

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str | None = None
    email_from: str | None = None
    email_to: list[str] = Field(default_factory=list)
    webhook_url: str | None = None
    has_smtp_password: bool = False
    has_webhook_token: bool = False
