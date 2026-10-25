"""Application services preserve durability and support dependency injection."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from conftest import login
from dbapi import execute, fetch_records
from oracle_fakes import remote_connection
from pydantic import ValidationError

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.oracle_unavailable import OracleUnavailable
from oraexport.models.credential import Credential
from oraexport.models.delivery import Delivery
from oraexport.models.event_details import EventDetails
from oraexport.models.job_status import JobStatus
from oraexport.models.notification_settings import NotificationSettings
from oraexport.models.provision_input import ProvisionInput
from oraexport.models.queued_run_snapshot import QueuedRunSnapshot
from oraexport.models.run import Run
from oraexport.models.run_snapshot import RunSnapshot
from oraexport.models.target import Target
from oraexport.models.target_catalog_response import TargetCatalogResponse
from oraexport.models.user import User
from oraexport.notifications import NotificationService
from oraexport.oracle_database import OracleDatabase
from oraexport.worker import Worker


@pytest.mark.parametrize(("mode", "schemas"), [("SCHEMA", ["HR", "FINANCE"]), ("FULL", [])])
def test_enqueue_persists_typed_immutable_snapshot(db, task, mode, schemas):
    """New snapshots retain the execution JSON contract and detach task settings.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        mode (str): Export scope to queue.
        schemas (list[str]): Selected schemas for the export.
    """
    task.export_mode = mode
    execute(
        db,
        ("UPDATE tasks SET export_mode = :export_mode WHERE id = :record_key"),
        {"export_mode": task.export_mode, "record_key": task.id},
    )
    task.schema_names = json.dumps(schemas)
    execute(
        db,
        ("UPDATE tasks SET schema_names = :schema_names WHERE id = :record_key"),
        {"schema_names": task.schema_names, "record_key": task.id},
        clobs=("schema_names",),
    )
    task.schema_name = schemas[0] if schemas else ""
    execute(
        db,
        ("UPDATE tasks SET schema_name = :schema_name WHERE id = :record_key"),
        {"schema_name": task.schema_name, "record_key": task.id},
    )
    run = OracleDatabase(task.target_id, db).enqueue(task)
    snapshot = QueuedRunSnapshot.model_validate_json(run.snapshot)
    assert snapshot.schema_names == tuple(schemas)
    assert snapshot.export_mode == mode
    assert snapshot.file_timezone == "UTC"
    run = next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                {"record_key": run.id},
            )
        ),
        run,
    )
    assert RunSnapshot.from_json(run.snapshot).options() == snapshot.model_dump(mode="json")
    task.parallel = 8
    execute(
        db,
        ("UPDATE tasks SET parallel = :parallel WHERE id = :record_key"),
        {"parallel": task.parallel, "record_key": task.id},
    )
    task.schema_names = '["CHANGED"]'
    execute(
        db,
        ("UPDATE tasks SET schema_names = :schema_names WHERE id = :record_key"),
        {"schema_names": task.schema_names, "record_key": task.id},
        clobs=("schema_names",),
    )
    assert snapshot.parallel == 1
    assert snapshot.schema_names == tuple(schemas)
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.parallel = 8
    with pytest.raises(ValidationError, match="extra_forbidden"):
        QueuedRunSnapshot.model_validate({**snapshot.model_dump(), "typo": 1})


def test_enqueue_rejects_invalid_snapshot_before_persistence(db, task):
    """Invalid persisted task settings cannot become new execution requests.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
    task.parallel = 0
    execute(
        db,
        ("UPDATE tasks SET parallel = :parallel WHERE id = :record_key"),
        {"parallel": task.parallel, "record_key": task.id},
    )
    with pytest.raises(ValidationError, match="parallel"):
        OracleDatabase(task.target_id, db).enqueue(task)
    assert (
        fetch_records(
            db,
            Run,
            ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"),
            {},
        )
        == []
    )


def test_executor_commits_intent_before_start_and_outcome_before_master_cleanup(factory, db, task):
    """Remote side effects observe durable state, and repeated startup is refused.

    Args:
        factory (Callable[[], Connection]): Metadata session factory for the worker.
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model record.
    """
    # Enqueue a new backup run and claim it through the worker
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    run = next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                {"record_key": run.id},
            )
        ),
        run,
    )
    assert Worker(factory).claim_run(db) == run.id
    calls = []

    class Remote:
        """Inspect committed metadata at the remote-operation boundaries."""

        def start(self, job, run_id, options):
            """Require persisted identity, intent, and snapshot before START_JOB.

            Args:
                job (str): Oracle Data Pump job name.
                run_id (str): Run record unique identifier.
                options (dict[str, Any]): Run options snapshot dictionary.
            """
            # Verify executor committed 'starting' state before invoking remote Data Pump
            saved = next(row for row in db.saved["runs"] if row["id"] == run_id)
            assert saved["state"] == "starting"
            assert saved["oracle_job"] == job
            assert json.loads(saved["snapshot"]) == options
            calls.append("start")

        def status(self, job):
            """Return explicit completion evidence.

            Args:
                job (str): Oracle Data Pump job name.

            Returns:
                JobStatus: Job status indicating completion at 100%.
            """
            return JobStatus(state="COMPLETED", progress=100)

        def read_log(self, directory, filename):
            """Return no additional completion evidence.

            Args:
                directory (str): Oracle directory object name.
                filename (str): Log filename.

            Returns:
                list[str]: Empty list of additional log lines.
            """
            return []

        def file_size(self, directory, filename):
            """Return a measured size for the recorded log.

            Args:
                directory (str): Oracle directory object name.
                filename (str): Log filename.

            Returns:
                int: Simulated file size in bytes.
            """
            return 100

        def release_master(self, job):
            """Require terminal history to be committed before master removal.

            Args:
                job (str): Oracle Data Pump job name.
            """
            # Ensure metadata commit happened before dropping the Data Pump master table
            saved = next(row for row in db.saved["runs"] if row["id"] == run.id)
            assert saved["state"] == "succeeded"
            assert saved["finished_at"] is not None
            calls.append("release")

    @contextmanager
    def connection(dsn, credential):
        """Yield an explicitly injected adapter without modifying worker globals.

        Args:
            dsn (str): Target Oracle DSN string.
            credential (Credential): Oracle database authentication credentials.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(Remote())

    executor = OracleDatabase(task.target_id, db, connection_factory=connection)
    executor.start_export(run.id)
    # Repeated start invocation must be idempotent and not trigger a second remote start
    executor.start_export(run.id)
    assert calls == ["start"]
    run = next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                {"record_key": run.id},
            )
        ),
        run,
    )
    assert executor.monitor_export(run.id)
    assert calls == ["start", "release"]


def test_provisioning_persists_generated_credentials_before_ddl_and_reuses_on_retry(db, task):
    """An interrupted provisioning operation retains the only generated password.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model record.
    """
    target = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": task.target_id},
            )
        ),
        None,
    )
    assert target is not None
    target.export_credential_id = None
    execute(
        db,
        ("UPDATE targets SET export_credential_id = :export_credential_id WHERE id = :record_key"),
        {"export_credential_id": target.export_credential_id, "record_key": target.id},
    )
    target.provisioned = False
    execute(
        db,
        ("UPDATE targets SET provisioned = :provisioned WHERE id = :record_key"),
        {"provisioned": target.provisioned, "record_key": target.id},
    )
    db.commit()
    actor = next(
        iter(
            fetch_records(
                db,
                User,
                ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE role = :role FETCH NEXT :row_limit ROWS ONLY"),
                dict({"role": "admin"}, row_limit=1),
            )
        ),
        None,
    )
    passwords = []

    class Remote:
        """Interrupt DDL once after checking durable credentials."""

        def catalog(self):
            """Accept the target catalog check before allocating an account.

            Returns:
                TargetCatalogResponse: Simulated target catalog.
            """
            return TargetCatalogResponse(kind="pdb", container_name="FINANCE", version="19c", edition="Enterprise")

        def account_exists(self, username):
            """Model an account that was never created by interrupted DDL.

            Args:
                username (str): Oracle username to verify.

            Returns:
                bool: Always False indicating account was not yet created.
            """
            return False

        def provision(self, options, password):
            """Observe the committed encrypted credential before remote DDL.

            Args:
                options (ProvisionInput): Provisioning options model.
                password (str | None): Plaintext password to provision.

            Raises:
                OracleUnavailable: On first attempt to simulate connection loss during DDL.
            """
            # Decompose multi-line generator lookups into single-line definitions
            assert target is not None
            saved_targets = db.saved["targets"]
            saved_target = next(row for row in saved_targets if row["id"] == target.id)
            saved_creds = db.saved["credentials"]
            exp_cred_id = saved_target["export_credential_id"]
            saved_credential = next(row for row in saved_creds if row["id"] == exp_cred_id)
            assert EncryptionSerivce.decrypt(saved_credential["ciphertext"]) == password
            passwords.append(password)
            if len(passwords) == 1:
                raise OracleUnavailable("DPY-4011")

        def verify_directory(self, directory):
            """Accept the existing fixture directory.

            Args:
                directory (str): Oracle directory object name.
            """
            assert directory == "EXPORT_DIR"

    @contextmanager
    def connection(dsn, credential):
        """Yield the injected provisioning transport.

        Args:
            dsn (str): Target Oracle DSN string.
            credential (Credential): Administrative credentials for provisioning.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(Remote())

    assert actor is not None
    assert target is not None
    service = OracleDatabase(task.target_id, db, connection_factory=connection)
    options = ProvisionInput(directory="EXPORT_DIR")
    # First attempt fails with simulated network interruption
    with pytest.raises(OracleUnavailable):
        service.provision_apply(options, actor)
    target = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": target.id},
            )
        ),
        target,
    )
    credential_id = target.export_credential_id
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Credential,
                    ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                    {"record_key": credential_id},
                )
            ),
            None,
        )
        is not None
    )
    assert not target.provisioned
    # Retry reuses the previously generated and persisted credential password
    service.provision_apply(options, actor)
    target = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": target.id},
            )
        ),
        target,
    )
    assert target.provisioned
    assert target.export_credential_id == credential_id
    assert passwords[0] == passwords[1]


def test_notification_transport_injects_senders_and_rejects_unknown_channel():
    """Channel selection is independent of network transports and outbox retries."""
    calls = []

    class Sender:
        """Record the selected delivery channel."""

        def send(self, config, secrets, delivery):
            """Record a transport invocation without sending a message.

            Args:
                config (dict[str, Any]): Notification channel configuration dictionary.
                secrets (dict[str, Any]): Notification credentials and secret tokens.
                delivery (Delivery): Outbox delivery entity being dispatched.
            """
            calls.append(delivery.id)

    # Inject mock sender into transport router
    transport = NotificationService(senders={"webhook": Sender()})
    event = Delivery(channel="webhook", payload='{"event":"test"}')
    transport.deliver({}, {}, event)
    assert calls == [event.id]
    # Unregistered channel types trigger clear configuration errors
    event.channel = "unknown"
    with pytest.raises(ValueError, match="channel"):
        transport.deliver({}, {}, event)


def test_notification_staging_uses_caller_transaction(db):
    """Both channels share an event payload and disappear on caller rollback.

    Args:
        db (Connection): Metadata database session.
    """
    config = {
        "smtp_host": "smtp.example",
        "email_to": ["operator@example.com"],
        "webhook_url": "https://example/event",
    }
    execute(
        db,
        ("INSERT INTO notification_settings (id, config, secrets) VALUES (:id, :config, :secrets)"),
        NotificationSettings(id=1, config=json.dumps(config)).model_dump(mode="python"),
        clobs=("config", "secrets"),
    )
    db.commit()
    timestamp = datetime(2026, 10, 14, 12)
    service = NotificationService(clock=lambda: timestamp)
    service.queue_event(db=db, event="export_failed", details=EventDetails(root={"run_id": "test"}))
    entries = list(
        fetch_records(
            db,
            Delivery,
            ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries"),
            {},
        )
    )
    assert {entry.channel for entry in entries} == {"email", "webhook"}
    assert all(
        json.loads(entry.payload)
        == {
            "event": "export_failed",
            "details": {"run_id": "test"},
            "timestamp": "2026-10-14T12:00:00Z",
        }
        for entry in entries
    )
    db.rollback()
    assert not fetch_records(
        db,
        Delivery,
        ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries"),
        {},
    )


def test_notification_dispatch_routes_due_events_and_commits_attempts(db):
    """A failed channel does not prevent successful delivery of the next event.

    Args:
        db (Connection): Metadata database session.
    """
    timestamp = datetime(2026, 10, 14, 12)
    config = {"webhook_url": "https://example/event"}
    secrets = {"webhook_token": "test-token"}
    execute(
        db,
        ("INSERT INTO notification_settings (id, config, secrets) VALUES (:id, :config, :secrets)"),
        NotificationSettings(id=1, config=json.dumps(config), secrets=EncryptionSerivce.encrypt(json.dumps(secrets))).model_dump(mode="python"),
        clobs=("config", "secrets"),
    )
    failed = Delivery(channel="unknown", payload="{}", next_attempt_at=timestamp - timedelta(seconds=2))
    delivered = Delivery(
        channel="webhook",
        payload="{}",
        next_attempt_at=timestamp - timedelta(seconds=1),
        error="old error",
    )
    future = Delivery(channel="webhook", payload="{}", next_attempt_at=timestamp + timedelta(seconds=1))
    for entry in (failed, delivered, future):
        execute(
            db,
            ("INSERT INTO notification_deliveries (id, channel, payload, state, attempts, next_attempt_at, error) VALUES (:id, :channel, :payload, :state, :attempts, :next_attempt_at, :error)"),
            entry.model_dump(mode="python"),
            clobs=("payload", "error"),
        )
    db.commit()
    calls = []

    class Sender:
        """Record a successful channel delivery."""

        def send(self, config, secrets, delivery):
            """Capture decrypted settings and delivery identity.

            Args:
                config (dict): Public channel settings.
                secrets (dict): Decrypted channel credentials.
                delivery (Delivery): Outbox event being sent.
            """
            calls.append((config, secrets, delivery.id))

    service = NotificationService(clock=lambda: timestamp, senders={"webhook": Sender()})
    service.process_outbox(db, limit=1)
    failed = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": failed.id},
            )
        ),
        failed,
    )
    assert failed.state == "pending"
    assert failed.attempts == 1
    failed = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": failed.id},
            )
        ),
        failed,
    )
    assert failed.next_attempt_at == timestamp + timedelta(seconds=60)
    failed = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": failed.id},
            )
        ),
        failed,
    )
    assert failed.error == "ValueError"
    assert not calls
    service.process_outbox(db)
    delivered = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": delivered.id},
            )
        ),
        delivered,
    )
    assert calls == [(config, secrets, delivered.id)]
    delivered = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": delivered.id},
            )
        ),
        delivered,
    )
    assert delivered.state == "delivered"
    assert delivered.attempts == 1
    delivered = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": delivered.id},
            )
        ),
        delivered,
    )
    assert delivered.error is None
    future = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": future.id},
            )
        ),
        future,
    )
    assert future.state == "pending"
    assert future.attempts == 0
    db.rollback()
    delivered = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": delivered.id},
            )
        ),
        delivered,
    )
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Delivery,
                    ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                    {"record_key": delivered.id},
                )
            ),
        ).state
        == "delivered"
    )
    failed = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": failed.id},
            )
        ),
        failed,
    )
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Delivery,
                    ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                    {"record_key": failed.id},
                )
            ),
        ).attempts
        == 1
    )


def test_api_uses_database_workflow(client, task, monkeypatch):
    """The route retains authorization and serialization with a substituted workflow.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Scoped workflow replacement.
    """
    calls = []

    def queue_export(self, task_id, user):
        """Record authorization and enqueue through the database's metadata session."""
        calls.append((task_id, user.role))
        assert self.target_id == task.target_id
        return self.enqueue(task=task)

    monkeypatch.setattr(target=OracleDatabase, name="queue_export", value=queue_export)
    login(client=client, role="operator")
    response = client.post(url=f"/api/v1/tasks/{task.id}/run")
    assert response.status_code == 202
    assert calls == [(task.id, "operator")]
    assert "credential_id" not in response.text
    assert "dsn" not in response.text


def test_notification_service_send_webhook_dispatches_http():
    """NotificationService delivers webhooks with authorization headers and bounded timeout."""
    service = NotificationService()
    delivery = Delivery(id="del-123", channel="webhook", payload='{"event": "export_completed"}')
    config = {"webhook_url": "https://example.com/webhook"}
    secrets = {"webhook_token": "secret-token-xyz"}
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        service.send_webhook(config, secrets, delivery)
        mock_client.post.assert_called_once_with(
            "https://example.com/webhook",
            json={"event": "export_completed"},
            headers={"X-OraExport-Delivery": "del-123", "Authorization": "Bearer secret-token-xyz"},
        )


def test_notification_service_send_email_dispatches_smtp():
    """NotificationService formats event email and dispatches over SMTP."""
    service = NotificationService()
    delivery = Delivery(id="del-456", channel="email", payload='{"event": "export_failed"}')
    config = {
        "smtp_host": "smtp.test.com",
        "smtp_port": 587,
        "email_from": "noreply@test.com",
        "email_to": ["admin@test.com"],
        "smtp_username": "smtpuser",
        "smtp_starttls": True,
    }
    secrets = {"smtp_password": "smtppassword"}
    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
        service.send_email(config, secrets, delivery)
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("smtpuser", "smtppassword")
        mock_smtp.send_message.assert_called_once()


def test_notification_service_default_senders():
    """Default NotificationService routes email and webhook channels and supports send."""
    service = NotificationService()
    delivery_email = Delivery(id="del-789", channel="email", payload='{"event": "test"}')
    delivery_webhook = Delivery(id="del-790", channel="webhook", payload='{"event": "test"}')
    with patch.object(service, "send_email") as mock_email, patch.object(service, "send_webhook") as mock_webhook:
        service.send({}, {}, delivery_email)
        mock_email.assert_called_once_with({}, {}, delivery_email)
        service.deliver({}, {}, delivery_webhook)
        mock_webhook.assert_called_once_with({}, {}, delivery_webhook)
