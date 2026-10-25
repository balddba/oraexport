"""Metadata repository behavior across shared transactions and locking boundaries."""

from datetime import datetime, timedelta, timezone
from typing import cast

import oracledb
from dbapi import Connection, execute, fetch_records
from pydantic import SecretStr

from oraexport.metadata_service import MetadataService
from oraexport.models.backup_file import BackupFile
from oraexport.models.discovery_response import DiscoveryResponse
from oraexport.models.run import Run
from oraexport.models.target_catalog_response import TargetCatalogResponse
from oraexport.models.task import Task
from oraexport.models.user import User
from oraexport.oracle_database import OracleDatabase


def test_explicit_projections_ignore_unrelated_columns(db: Connection, task: Task) -> None:
    """Keep record reads stable when tables gain columns or change column order.

    Args:
        db (Connection): Isolated metadata database session.
        task (Task): Persisted task used to seed an export and its manifest.
    """
    # The recorder implements the driver subset exercised by these workflows.
    metadata_connection = cast(oracledb.Connection, db)
    repository = MetadataService(metadata_connection)
    run = OracleDatabase(task.target_id, metadata_connection).enqueue(task)
    expected_users = repository.users()
    expected_files = repository.files(run.id)
    for table in ("app_users", "tasks", "runs", "backup_files"):
        for row in db.tables[table]:
            original = list(row.items())
            row.clear()
            row["future_column"] = "unrelated value"
            row.update(reversed(original))

    assert repository.users() == expected_users
    assert repository.tasks() == [task]
    assert repository.runs() == [run]
    assert repository.get_for_update(run.id) == run
    assert repository.files(run.id) == expected_files
    assert list(repository.run_batches()) == [[run]]


def test_repository_writes_and_audit_share_caller_rollback(db):
    """Roll back account changes and audit entries in the caller's transaction.

    Args:
        db (Connection): Isolated metadata database session.
    """
    repository = MetadataService(db)
    account = User(username="temporary", password_hash="hash")
    execute(
        db,
        ("INSERT INTO app_users (id, username, password_hash, role, enabled) VALUES (:id, :username, :password_hash, :role, :enabled)"),
        account.model_dump(mode="python"),
    )
    repository.record_audit(account, "created", account.id)
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    User,
                    ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE id = :record_key"),
                    {"record_key": account.id},
                )
            ),
            None,
        )
        == account
    )
    assert len(repository.audit(1)) == 1
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    User,
                    ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE id = :record_key"),
                    {"record_key": account.id},
                )
            ),
            None,
        )
        is None
    )
    assert next(iter([row[0] for row in execute(db, "SELECT COUNT(*) FROM audit_events", {})]), None) == 0


def test_repository_history_filters_and_count_match(db, task):
    """Normalize history timestamps and apply matching count and pagination filters.

    Args:
        db (Connection): Isolated metadata database session.
        task (Task): Persisted export task used to create run history.
    """
    repository = MetadataService(db)
    for index, state in enumerate(("queued", "running", "succeeded")):
        execute(
            db,
            (
                "INSERT INTO runs (id, task_id, target_id, occurrence, "
                "snapshot, state, oracle_job, created_at, started_at, "
                "finished_at, progress, errors, messages, "
                "cancel_requested, last_seen_at, notification_created) "
                "VALUES (:id, :task_id, :target_id, :occurrence, "
                ":snapshot, :state, :oracle_job, :created_at, "
                ":started_at, :finished_at, :progress, :errors, "
                ":messages, :cancel_requested, :last_seen_at, "
                ":notification_created)"
            ),
            Run(
                task_id=task.id,
                target_id=task.target_id,
                state=state,
                occurrence=f"test:{index}",
                snapshot="{}",
                oracle_job=f"TEST_{index}",
                created_at=datetime(2026, 10, 14, 12, index),
            ).model_dump(mode="python"),
            clobs=("snapshot", "errors", "messages"),
        )
    db.commit()
    since = datetime(year=2026, month=10, day=14, hour=8, tzinfo=timezone(offset=timedelta(hours=-4)))
    assert repository.run_count(state="active", task_id=task.id, target_id=task.target_id, since=since) == 1
    assert [run.state for run in repository.runs(state="active", task_id=task.id, target_id=task.target_id, since=since, limit=1)] == ["running"]
    assert repository.runs(state="active", task_id=task.id, target_id=task.target_id, since=since, offset=1, limit=1) == []
    assert db.statements[-1][1]["since"] == datetime(year=2026, month=10, day=14, hour=12)

    # Inclusive date bounds use UTC for both list and streaming queries.
    boundary = datetime(year=2026, month=10, day=14, hour=8, minute=1, tzinfo=timezone(offset=timedelta(hours=-4)))
    listed = repository.runs(task_id=task.id, target_id=task.target_id, since=boundary, until=boundary)
    assert [run.state for run in listed] == ["running"]
    assert repository.run_count(task_id=task.id, target_id=task.target_id, since=boundary, until=boundary) == 1
    assert list(repository.run_batches(task_id=task.id, target_id=task.target_id, since=boundary, until=boundary, batch_size=1)) == [listed]
    assert [run.state for batch in repository.run_batches(state="active", since=since) for run in batch] == ["running"]
    assert [run.state for run in repository.runs(state="succeeded")] == ["succeeded"]
    assert repository.run_count(state="succeeded") == 1
    assert repository.runs(task_id="another-task") == []
    assert repository.run_count(target_id="another-target") == 0
    assert list(repository.run_batches(target_id="another-target")) == []


def test_repository_manifest_lock_refreshes_current_state(db, task):
    """Reload current manifest values under a row lock without a fetch clause.

    Args:
        db (Connection): Isolated metadata database session.
        task (Task): Persisted export task used to enqueue a run.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    repository = MetadataService(db)
    file = repository.files(run.id)[0]
    execute(db, ("UPDATE backup_files SET cleanup_attempts = 3 WHERE id = :id"), {"id": file.id})
    updated = repository.file_for_update(file_id=file.id)
    assert updated is not None
    assert updated.cleanup_attempts == 3
    assert file.cleanup_attempts == 0
    statement = next(sql for sql, _ in reversed(db.statements) if "FROM backup_files" in sql)
    assert statement.endswith("FOR UPDATE")
    assert "FETCH" not in statement


def test_repository_retained_manifest_and_due_task_locks(db, task):
    """Exclude deleted files from retained manifests and lock due task rows.

    Args:
        db (Connection): Isolated metadata database session.
        task (Task): Persisted export task eligible for scheduling.
    """
    repository = MetadataService(db)
    run = OracleDatabase(task.target_id, db).enqueue(task)
    execute(
        db,
        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
        BackupFile(
            run_id=run.id,
            directory=task.directory,
            filename="deleted.dmp",
            kind="dump",
            deleted_at=datetime(2026, 10, 1),
        ).model_dump(mode="python"),
        clobs=("cleanup_error",),
    )
    task.next_run_at = datetime(2026, 10, 14, 12)
    execute(
        db,
        ("UPDATE tasks SET next_run_at = :next_run_at WHERE id = :record_key"),
        {"next_run_at": task.next_run_at, "record_key": task.id},
    )
    db.commit()
    assert len(repository.files(run.id)) == 2
    retained = repository.files(run.id, retained=True, lock=True)
    assert len(retained) == 1
    assert retained[0].deleted_at is None
    assert repository.due_tasks(datetime(2026, 10, 14, 12)) == [task]
    statement = next(sql for sql, _ in reversed(db.statements) if "FROM tasks" in sql)
    assert statement.endswith("FOR UPDATE")
    assert "FETCH" not in statement


def test_metadata_service_owns_registration_and_target_workflows(db):
    """Registration workflows share the service's transaction and injected remote driver.

    Args:
        db (Connection): Isolated metadata transaction.
    """
    from contextlib import contextmanager

    import pytest
    from oracle_fakes import remote_connection

    from oraexport.encryption_serivce import EncryptionSerivce
    from oraexport.exceptions.application_error import ApplicationError
    from oraexport.models.credential import Credential
    from oraexport.models.registration import Registration
    from oraexport.models.registration_input import RegistrationInput
    from oraexport.models.registration_update import RegistrationUpdate
    from oraexport.models.target import Target
    from oraexport.models.target_input import TargetInput

    class Remote:
        """Provide database discovery and the actual connected container."""

        def discover(self):
            """Return the registered database's topology."""
            return DiscoveryResponse(kind="cdb", version="19c")

        def catalog(self):
            """Return the target service's container and edition."""
            return TargetCatalogResponse(kind="pdb", container_name="FINANCE", version="19c", edition="Enterprise")

    connections = []

    @contextmanager
    def connect_remote(dsn, credential):
        """Capture remote connection inputs without contacting Oracle.

        Args:
            dsn (str): Database service address.
            credential (Credential): Saved administrative credential.

        Yields:
            RemoteConnection: Recording remote driver connection.
        """
        connections.append((dsn, credential.id))
        yield remote_connection(Remote())

    actor = fetch_records(db, User, "SELECT id, username, password_hash, role, enabled FROM app_users WHERE username = :username", {"username": "admin"})[0]
    service = MetadataService(db, connection_factory=connect_remote)
    registration = service.create_registration(
        RegistrationInput(name="Lifecycle DB", dsn="db:1521/cdb", username="DBA", password=SecretStr(secret_value="initial-secret")),
        actor,
    )
    previous_credential = registration.credential_id
    discovery = service.discover_registration(registration.id, actor)
    assert discovery.kind == "cdb"
    assert discovery.version == "19c"
    target = service.create_target(
        TargetInput(
            name="Lifecycle target",
            registration_id=registration.id,
            dsn="db:1521/finance",
            pdb_name="FINANCE",
        ),
        actor,
    )
    assert target.admin_credential_id == previous_credential
    assert connections == [
        (registration.dsn, previous_credential),
        (target.dsn, previous_credential),
    ]
    updated = service.update_registration(
        registration.id,
        RegistrationUpdate(
            name="Updated DB",
            dsn=registration.dsn,
            username="DBA",
            password=SecretStr(secret_value="rotated-secret"),
            sysdba=False,
        ),
        actor,
    )
    saved_target = fetch_records(db, Target, "SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :id", {"id": target.id})[0]
    assert saved_target.admin_credential_id == updated.credential_id != previous_credential
    assert updated.kind == "unknown"
    assert updated.version is None
    credentials = fetch_records(db, Credential, "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :id", {"id": updated.credential_id})
    assert EncryptionSerivce.decrypt(credentials[0].ciphertext) == "rotated-secret"
    assert not execute(db, "SELECT id FROM credentials WHERE id = :id", {"id": previous_credential})
    with pytest.raises(ApplicationError, match="export targets"):
        service.delete_registration(registration.id, actor)
    assert service.delete_target(target.id, actor) == {"ok": True}
    assert service.delete_registration(registration.id, actor) == {"ok": True}
    assert not fetch_records(db, Registration, "SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :id", {"id": registration.id})
    assert not execute(db, "SELECT id FROM credentials WHERE id = :id", {"id": updated.credential_id})
