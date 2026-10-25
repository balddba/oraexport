"""NotificationInput Pydantic model."""

from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator

from .strict_model import StrictModel


class NotificationInput(StrictModel):
    """Email/webhook settings; omitted passwords preserve stored secrets.

    Attributes:
        smtp_host (str | None): SMTP server hostname or IP address.
        smtp_port (int): SMTP server port number (1-65535).
        smtp_starttls (bool): Flag indicating STARTTLS encryption requirement.
        smtp_username (str | None): Optional SMTP authentication username.
        smtp_password (SecretStr | None): Optional SMTP authentication password.
        email_from (str | None): Sender email address.
        email_to (list[str]): List of recipient email addresses.
        webhook_url (str | None): HTTPS webhook endpoint URL.
        webhook_token (SecretStr | None): Optional secret token for webhook authorization.
    """

    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_starttls: bool = True
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    email_from: str | None = None
    email_to: list[str] = Field(default_factory=list)
    webhook_url: str | None = None
    webhook_token: SecretStr | None = None

    @field_validator("webhook_url")
    @classmethod
    def secure_webhook(cls, value: str | None) -> str | None:
        """Require HTTPS webhooks without embedded credentials.

        Args:
            value (str | None): Candidate webhook URL.

        Returns:
            str | None: Validated HTTPS webhook URL or None.

        Raises:
            ValueError: If URL does not use HTTPS or embeds credentials.
        """
        if value:
            parsed = urlparse(url=value)
            is_https = parsed.scheme == "https"
            has_host = bool(parsed.hostname)
            has_user = bool(parsed.username)
            if not is_https or not has_host or has_user:
                raise ValueError("Webhook must use HTTPS without embedded credentials")
        return value

    @field_validator("email_from", "email_to")
    @classmethod
    def email_headers(cls, value: str | list[str] | None) -> str | list[str] | None:
        """Reject email header injection and malformed recipients.

        Args:
            value (str | list[str] | None): Single email or list of email addresses.

        Returns:
            str | list[str] | None: Validated email address(es).

        Raises:
            ValueError: If any email address contains newline characters or lacks @ symbol.
        """
        address_list = value if isinstance(value, list) else [value]
        for address in address_list:
            if address:
                has_at = "@" in address
                has_newline = "\n" in address or "\r" in address
                if not has_at or has_newline:
                    raise ValueError("Invalid email address")
        return value
