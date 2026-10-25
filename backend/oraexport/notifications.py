"""NotificationService workflows with explicit Oracle transaction boundaries."""

import json
import smtplib
import ssl
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx
import oracledb
from oracledb import Connection

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.safe_error import safe_error
from oraexport.metadata_service import MetadataService
from oraexport.models.defaults import now
from oraexport.models.delivery import Delivery
from oraexport.models.event_details import EventDetails
from oraexport.models.notification_input import NotificationInput
from oraexport.models.notification_settings import NotificationSettings
from oraexport.models.notification_settings_response import NotificationSettingsResponse
from oraexport.models.status_response import StatusResponse
from oraexport.models.user import User
from oraexport.types import NotificationSender

DeliverySender = Callable[[Mapping[str, object], dict[str, str], Delivery], None]


class NotificationService:
    """Coordinate notifications workflows within explicit metadata transactions.

    Manages notification channels (SMTP email, HTTPS webhooks), safely persists
    and encrypts sensitive delivery credentials, exposes public configuration
    without leaking secrets, stages events into the durable outbox, routes deliveries
    to channel senders, and processes retries with independent per-attempt commits.
    """

    def __init__(
        self,
        sender: DeliverySender | None = None,
        clock: Callable[[], datetime] = now,
        senders: Mapping[str, NotificationSender | DeliverySender] | None = None,
    ) -> None:
        """Inject channel senders, delivery dispatch, and the notification clock.

        Args:
            sender (DeliverySender | None): Optional override for delivery dispatch.
            clock (Callable[[], datetime]): Timestamp source for staging and retries.
            senders (dict[str, NotificationSender | DeliverySender] | None): Optional senders map.
        """
        self.clock = clock
        if senders is not None:
            self.senders = senders
        else:
            self.senders = {"email": self._dispatch_email, "webhook": self._dispatch_webhook}
        if sender is not None:
            self.sender = sender
        else:
            self.sender = self.deliver

    def notification_settings(self, db: Connection, user: User) -> NotificationSettingsResponse:
        """Read public notification configuration and secret-presence indicators.

        Args:
            db (Connection): Active metadata database session for reading settings.
            user (User): Authenticated user requesting the notification configuration.

        Returns:
            NotificationSettingsResponse: Settings model with masked secrets and presence flags.
        """
        with db.cursor() as cursor:
            cursor.execute("SELECT id, config, secrets FROM notification_settings WHERE id = :record_key", {"record_key": 1})
            rows = [NotificationSettings(id=row[0], config=row[1].read() if hasattr(row[1], "read") else row[1], secrets=row[2].read() if hasattr(row[2], "read") else row[2]) for row in cursor.fetchall()]
        settings = rows[0] if rows else None
        config = json.loads(s=settings.config) if settings else {}
        saved = json.loads(s=EncryptionSerivce.decrypt(value=settings.secrets)) if settings and settings.secrets else {}
        # Mask credentials: never expose raw passwords/tokens; expose presence flags only.
        result = NotificationSettingsResponse.model_validate(obj=config)
        result.has_smtp_password = bool(saved.get("smtp_password"))
        result.has_webhook_token = bool(saved.get("webhook_token"))
        return result

    def save_notifications(self, body: NotificationInput, db: Connection, user: User) -> StatusResponse:
        """Save notification endpoints while keeping omitted secrets intact.

        Args:
            body (NotificationInput): Validated payload with endpoints and optional secrets.
            db (Connection): Transaction for saving configuration and secrets.
            user (User): Authenticated administrator saving the notification settings.

        Returns:
            StatusResponse: Response containing ok status flag.

        Raises:
            ValueError: If SMTP configuration is missing a sender or recipient.
        """
        # Validate that SMTP sender and recipient are provided when host is set.
        if body.smtp_host and (not body.email_from or not body.email_to):
            raise ValueError("SMTP requires a sender and at least one recipient")
        with db.cursor() as cursor:
            cursor.execute("SELECT id, config, secrets FROM notification_settings WHERE id = :id", {"id": 1})
            rows = [NotificationSettings(id=row[0], config=row[1].read() if hasattr(row[1], "read") else row[1], secrets=row[2].read() if hasattr(row[2], "read") else row[2]) for row in cursor.fetchall()]
        settings = rows[0] if rows else None
        creating = settings is None
        settings = settings or NotificationSettings(id=1)
        saved = json.loads(s=EncryptionSerivce.decrypt(value=settings.secrets)) if settings.secrets else {}
        # Preserve existing secrets when omitted in partial update payloads.
        for key in ("smtp_password", "webhook_token"):
            value = getattr(body, key)
            if value is not None:
                saved[key] = value.get_secret_value()
        # Separate public configuration from encrypted credential payload.
        settings.config = json.dumps(
            obj={
                "smtp_host": body.smtp_host,
                "smtp_port": body.smtp_port,
                "smtp_starttls": body.smtp_starttls,
                "smtp_username": body.smtp_username,
                "email_from": body.email_from,
                "email_to": body.email_to,
                "webhook_url": body.webhook_url,
            }
        )
        settings.secrets = EncryptionSerivce.encrypt(value=json.dumps(obj=saved))
        if creating:
            with db.cursor() as cursor:
                cursor.setinputsizes(config=oracledb.DB_TYPE_CLOB, secrets=oracledb.DB_TYPE_CLOB)
                parameters = {
                    "id": settings.id,
                    "config": settings.config,
                    "secrets": settings.secrets,
                }
                cursor.execute(
                    ("INSERT INTO notification_settings (id, config, secrets) VALUES (:id, :config, :secrets)"),
                    parameters,
                )
        else:
            with db.cursor() as cursor:
                cursor.setinputsizes(config=oracledb.DB_TYPE_CLOB, secrets=oracledb.DB_TYPE_CLOB)
                parameters = {
                    "id": settings.id,
                    "config": settings.config,
                    "secrets": settings.secrets,
                }
                cursor.execute(
                    ("UPDATE notification_settings SET config = :config, secrets = :secrets WHERE id = :id"),
                    parameters,
                )
        # Stage audit event and commit transaction boundary atomically.
        MetadataService(db=db).record_audit(user=user, action="notifications_updated", resource_id="1")
        db.commit()
        return StatusResponse(ok=True)

    def test_notifications(self, db: Connection, user: User) -> StatusResponse:
        """Queue a test event through the normal durable delivery path.

        Args:
            db (Connection): Active metadata database session for staging test outbox event.
            user (User): Authenticated administrator requesting delivery test.

        Returns:
            StatusResponse: Response containing ok status flag.

        Raises:
            ValueError: If neither email nor webhook channel has been configured.
        """
        with db.cursor() as cursor:
            cursor.execute("SELECT id, config, secrets FROM notification_settings WHERE id = :record_key", {"record_key": 1})
            rows = [NotificationSettings(id=row[0], config=row[1].read() if hasattr(row[1], "read") else row[1], secrets=row[2].read() if hasattr(row[2], "read") else row[2]) for row in cursor.fetchall()]
        settings = rows[0] if rows else None
        config = json.loads(s=settings.config) if settings else {}
        # Confirm at least one destination transport channel is configured.
        has_webhook = bool(config.get("webhook_url"))
        has_smtp = bool(config.get("smtp_host") and config.get("email_to"))
        if not (has_webhook or has_smtp):
            raise ValueError("Configure an email or webhook channel before sending a test")
        # Enqueue event into the durable outbox to exercise production delivery pipeline.
        test_payload = EventDetails(root={"requested_by": user.username})
        self.queue_event(db=db, event="test_notification", details=test_payload)
        db.commit()
        return StatusResponse(ok=True)

    def queue_event(self, db: Connection, event: str, details: EventDetails) -> None:
        """Create one outbox delivery for each configured channel.

        Args:
            db (Connection): Active metadata database session for staging delivery rows.
            event (str): Standard event type name (such as export_failed, test_notification).
            details (EventDetails): Validated event details object.

        Returns:
            None: Stages delivery records without committing the session.
        """
        with db.cursor() as cursor:
            cursor.execute("SELECT id, config, secrets FROM notification_settings WHERE id = :record_key", {"record_key": 1})
            rows = [NotificationSettings(id=row[0], config=row[1].read() if hasattr(row[1], "read") else row[1], secrets=row[2].read() if hasattr(row[2], "read") else row[2]) for row in cursor.fetchall()]
        settings = rows[0] if rows else None
        if not settings:
            return
        config = json.loads(s=settings.config)
        iso_ts = self.clock().isoformat() + "Z"
        payload_dict = {"event": event, "details": details.model_dump(mode="json"), "timestamp": iso_ts}
        payload = json.dumps(obj=payload_dict)
        # Stage channel-specific delivery rows in caller's active metadata transaction.
        if config.get("smtp_host") and config.get("email_to"):
            email_delivery = Delivery(channel="email", payload=payload)
            with db.cursor() as cursor:
                cursor.setinputsizes(payload=oracledb.DB_TYPE_CLOB, error=oracledb.DB_TYPE_CLOB)
                parameters = {
                    "id": email_delivery.id,
                    "channel": email_delivery.channel,
                    "payload": email_delivery.payload,
                    "state": email_delivery.state,
                    "attempts": email_delivery.attempts,
                    "next_attempt_at": email_delivery.next_attempt_at,
                    "error": email_delivery.error,
                }
                cursor.execute(
                    ("INSERT INTO notification_deliveries (id, channel, payload, state, attempts, next_attempt_at, error) VALUES (:id, :channel, :payload, :state, :attempts, :next_attempt_at, :error)"),
                    parameters,
                )
        if config.get("webhook_url"):
            webhook_delivery = Delivery(channel="webhook", payload=payload)
            with db.cursor() as cursor:
                cursor.setinputsizes(payload=oracledb.DB_TYPE_CLOB, error=oracledb.DB_TYPE_CLOB)
                parameters = {
                    "id": webhook_delivery.id,
                    "channel": webhook_delivery.channel,
                    "payload": webhook_delivery.payload,
                    "state": webhook_delivery.state,
                    "attempts": webhook_delivery.attempts,
                    "next_attempt_at": webhook_delivery.next_attempt_at,
                    "error": webhook_delivery.error,
                }
                cursor.execute(
                    ("INSERT INTO notification_deliveries (id, channel, payload, state, attempts, next_attempt_at, error) VALUES (:id, :channel, :payload, :state, :attempts, :next_attempt_at, :error)"),
                    parameters,
                )

    def _dispatch_email(self, config: Mapping[str, object], secrets: dict[str, str], delivery: Delivery) -> None:
        """Forward email dispatch to the active send_email implementation.

        Args:
            config (dict[str, object]): SMTP configuration dictionary.
            secrets (dict[str, str]): Secret credentials dictionary.
            delivery (Delivery): Outbox delivery record.
        """
        self.send_email(config, secrets, delivery)

    def _dispatch_webhook(self, config: Mapping[str, object], secrets: dict[str, str], delivery: Delivery) -> None:
        """Forward webhook dispatch to the active send_webhook implementation.

        Args:
            config (dict[str, object]): Webhook configuration dictionary.
            secrets (dict[str, str]): Secret credentials dictionary.
            delivery (Delivery): Outbox delivery record.
        """
        self.send_webhook(config, secrets, delivery)

    def send_webhook(self, config: Mapping[str, object], secrets: dict[str, str], delivery: Delivery) -> None:
        """Deliver one durable notification payload to an HTTPS webhook endpoint.

        Dispatches JSON event payloads to an external HTTPS endpoint with bounded
        timeouts and optional bearer token authorization.

        Args:
            config (dict[str, object]): Transport configuration containing the webhook_url string.
            secrets (dict[str, str]): Secret values containing an optional webhook_token.
            delivery (Delivery): Outbox delivery record providing the serialized payload and id.

        Raises:
            httpx.HTTPError: If the remote HTTP server returns an error code or times out.
        """
        payload = json.loads(s=delivery.payload)
        headers = {"X-OraExport-Delivery": delivery.id}
        token = secrets.get("webhook_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        target_url = str(config["webhook_url"])
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            client.post(target_url, json=payload, headers=headers).raise_for_status()

    def send_email(self, config: Mapping[str, object], secrets: dict[str, str], delivery: Delivery) -> None:
        """Deliver one durable notification using SMTP email transport.

        Formats structured event notifications into formatted email messages and
        transports them via an SMTP server using optional StartTLS encryption.

        Args:
            config (dict[str, object]): SMTP config with host, port, email_from, and email_to.
            secrets (dict[str, str]): Secret dictionary with optional smtp_password.
            delivery (Delivery): Outbox delivery record providing the serialized payload.

        Raises:
            smtplib.SMTPException: If the SMTP handshake, authentication, or dispatch fails.
            OSError: If the underlying network socket connection fails or times out.
        """
        payload = json.loads(s=delivery.payload)
        message = EmailMessage()
        event_name = payload["event"]
        message["Subject"] = f"OraExport: {event_name}"
        message["From"] = str(config["email_from"])
        recipients = config["email_to"]
        if not isinstance(recipients, list) or not all(isinstance(address, str) for address in recipients):
            raise ValueError("Email recipients must be a list of addresses")
        to_addresses = ", ".join(str(address) for address in recipients)
        message["To"] = to_addresses
        formatted_body = json.dumps(obj=payload, indent=2)
        message.set_content(formatted_body)
        smtp_host = str(config["smtp_host"])
        port = config["smtp_port"]
        if not isinstance(port, (str, int)):
            raise ValueError("SMTP port must be an integer")
        smtp_port = int(port)
        with smtplib.SMTP(host=smtp_host, port=smtp_port, timeout=10) as smtp:
            use_starttls = config.get("smtp_starttls", True)
            if use_starttls:
                tls_context = ssl.create_default_context()
                smtp.starttls(context=tls_context)
            username = config.get("smtp_username")
            if username:
                password = secrets.get("smtp_password", "")
                smtp.login(str(username), password)
            smtp.send_message(msg=message)

    def send(self, config: Mapping[str, object], secrets: dict[str, str], delivery: Delivery) -> None:
        """Send notification payload by routing to the configured channel handler.

        Args:
            config (dict[str, object]): Public notification configuration settings.
            secrets (dict[str, str]): Decrypted notification secrets dictionary.
            delivery (Delivery): Metadata delivery record containing channel and payload.

        Raises:
            ValueError: If the saved channel has no supported transport implementation.
        """
        self.deliver(config=config, secrets=secrets, delivery=delivery)

    def deliver(self, config: Mapping[str, object], secrets: dict[str, str], delivery: Delivery) -> None:
        """Dispatch an event to its configured channel sender.

        Args:
            config (dict[str, object]): Public notification configuration settings.
            secrets (dict[str, str]): Decrypted notification secrets dictionary.
            delivery (Delivery): Metadata delivery record containing channel and payload.

        Raises:
            ValueError: If the saved channel has no supported transport implementation.
        """
        if delivery.channel not in self.senders:
            raise ValueError("Unknown notification channel")
        sender = self.senders[delivery.channel]
        if isinstance(sender, NotificationSender):
            sender.send(config=config, secrets=secrets, delivery=delivery)
        else:
            sender(config, secrets, delivery)

    def process_outbox(self, db: Connection, limit: int = 5) -> None:
        """Attempt due deliveries; retain failed events after five attempts.

        Args:
            db (Connection): Active metadata database session for querying and updating deliveries.
            limit (int): Maximum number of due delivery attempts to process in one pass.

        Returns:
            None: Processes due deliveries and commits per-attempt status updates.
        """
        with db.cursor() as cursor:
            cursor.execute("SELECT id, config, secrets FROM notification_settings WHERE id = :record_key", {"record_key": 1})
            rows = [NotificationSettings(id=row[0], config=row[1].read() if hasattr(row[1], "read") else row[1], secrets=row[2].read() if hasattr(row[2], "read") else row[2]) for row in cursor.fetchall()]
        settings = rows[0] if rows else None
        if not settings:
            return
        config = json.loads(s=settings.config)
        secrets = json.loads(s=EncryptionSerivce.decrypt(value=settings.secrets)) if settings.secrets else {}
        # Fetch pending deliveries that are due for attempt based on next_attempt_at.
        entries = MetadataService(db=db).due_deliveries(current=self.clock(), limit=limit)
        for entry in entries:
            entry.attempts += 1
            with db.cursor() as cursor:
                parameters = {"attempts": entry.attempts, "record_key": entry.id}
                cursor.execute(
                    ("UPDATE notification_deliveries SET attempts = :attempts WHERE id = :record_key"),
                    parameters,
                )
            # Execute delivery attempt with independent transaction boundary per attempt.
            try:
                self.sender(config, secrets, entry)
                entry.state = "delivered"
                entry.error = None
                with db.cursor() as cursor:
                    cursor.setinputsizes(error=oracledb.DB_TYPE_CLOB)
                    parameters = {
                        "state": entry.state,
                        "error": entry.error,
                        "record_key": entry.id,
                    }
                    cursor.execute(
                        ("UPDATE notification_deliveries SET state = :state, error = :error WHERE id = :record_key"),
                        parameters,
                    )
            except Exception as exc:
                entry.error = safe_error(exc=exc)
                entry.state = "failed" if entry.attempts >= 5 else "pending"
                with db.cursor() as cursor:
                    cursor.setinputsizes(error=oracledb.DB_TYPE_CLOB)
                    parameters = {
                        "error": entry.error,
                        "state": entry.state,
                        "record_key": entry.id,
                    }
                    cursor.execute(
                        ("UPDATE notification_deliveries SET error = :error, state = :state WHERE id = :record_key"),
                        parameters,
                    )
                backoff_seconds = 30 * (2**entry.attempts)
                entry.next_attempt_at = self.clock() + timedelta(seconds=backoff_seconds)
                with db.cursor() as cursor:
                    parameters = {"next_attempt_at": entry.next_attempt_at, "record_key": entry.id}
                    cursor.execute(
                        ("UPDATE notification_deliveries SET next_attempt_at = :next_attempt_at WHERE id = :record_key"),
                        parameters,
                    )
            # Commit each delivery attempt immediately to isolate network failure impacts.
            db.commit()
