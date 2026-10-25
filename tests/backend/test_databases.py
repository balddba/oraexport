"""OracleDatabase ownership, captured export context, and scoped retention regressions."""

import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from dbapi import execute, fetch_records
from oracle_fakes import remote_connection

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.application_error import ApplicationError
from oraexport.models.backup_file import BackupFile
from oraexport.models.credential import Credential
from oraexport.models.defaults import uid
from oraexport.models.job_status import JobStatus
from oraexport.models.reconcile_input import ReconcileInput
from oraexport.models.run import Run
from oraexport.models.target import Target
from oraexport.models.task import Task
from oraexport.models.task_input import TaskInput
from oraexport.models.user import User
from oraexport.oracle_database import OracleDatabase
from oraexport.worker import Worker

CURRENT = datetime(2026, 10, 14, 12)


@pytest.mark.parametrize("fail_operation", [False, True])
def test_connected_database_clears_capabilities_and_closes_cursor(db, task, fail_operation):
    """Normal and failed operations release remote state before closing the connection."""
    driver = MagicMock()
    cursor = driver.cursor.return_value
    credential = next(
        iter(
            fetch_records(
                db,
                Credential,
                ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                {
                    "record_key": next(
                        iter(
                            fetch_records(
                                db,
                                Target,
                                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                                {"record_key": task.target_id},
                            )
                        ),
                    ).admin_credential_id
                },
            )
        ),
        None,
    )
    closed = []

    @contextmanager
    def connection(dsn, supplied_credential):
        """Record cleanup ordering at the driver connection boundary."""
        assert dsn == "test:1521/service"
        assert supplied_credential is credential
        try:
            yield driver
        finally:
            cursor.close.assert_called_once()
            closed.append(True)

    database = OracleDatabase(task.target_id, db, connection_factory=connection)
    for name in (
        "oracle_connection",
        "oracle_cursor",
    ):
        with pytest.raises(RuntimeError, match="connected"):
            getattr(database, name)
    assert credential is not None
    expectation = pytest.raises(ValueError, match=r"^operation failed$") if fail_operation else nullcontext()
    with expectation, database.connected("test:1521/service", credential) as connected:
        assert connected is database
        assert database.metadata_session is db
        assert database.oracle_cursor is cursor
        with pytest.raises(RuntimeError, match="already has"), database.connected("other:1521/service", credential):
            pytest.fail("Nested session must not open")
        assert database.oracle_connection is driver
        if fail_operation:
            raise ValueError("operation failed")
    assert closed == [True]
    for name in (
        "oracle_connection",
        "oracle_cursor",
    ):
        with pytest.raises(RuntimeError, match="connected"):
            getattr(database, name)


def test_connected_database_can_reconnect_after_cursor_initialization_failure(db, task):
    """Cursor initialization failure clears the connection and allows a new credential."""
    drivers = [MagicMock(), MagicMock()]
    drivers[0].cursor.side_effect = ValueError("cursor initialization failed")
    credentials = []

    @contextmanager
    def connection(dsn, credential):
        """Yield a fresh driver session for each credential."""
        credentials.append(credential)
        yield drivers[len(credentials) - 1]

    database = OracleDatabase(task.target_id, db, connection_factory=connection)
    first = Credential(username="ADMIN", ciphertext="test-ciphertext")
    second = Credential(username="EXPORT", ciphertext="test-ciphertext")
    with pytest.raises(ValueError, match="cursor initialization failed"), database.connected("test:1521/service", first):
        pytest.fail("Failed initialization must not yield")
    with pytest.raises(RuntimeError, match="connected"):
        _ = database.oracle_connection
    with database.connected("captured:1521/service", second):
        assert database.oracle_connection is drivers[1]
        assert database.oracle_cursor is drivers[1].cursor.return_value
    assert credentials == [first, second]
    drivers[1].cursor.return_value.close.assert_called_once()


@pytest.fixture
def other_task(db, task):
    """Seed a second database with its own backup task."""
    target = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": task.target_id},
            )
        ),
    ).model_copy(update={"id": uid(), "name": "Other DB"})
    execute(
        db,
        (
            "INSERT INTO targets (id, registration_id, name, dsn, "
            "admin_credential_id, export_credential_id, directory, "
            "tablespace, provisioned, edition, container_name) "
            "VALUES (:id, :registration_id, :name, :dsn, "
            ":admin_credential_id, :export_credential_id, "
            ":directory, :tablespace, :provisioned, :edition, "
            ":container_name)"
        ),
        target.model_dump(mode="python"),
    )

    other = task.model_copy(update={"id": uid(), "name": "Other task", "target_id": target.id})
    execute(
        db,
        (
            "INSERT INTO tasks (id, name, target_id, schema_name, "
            "directory, enabled, cron, timezone, retention_days, "
            "parallel, file_size_gb, content, consistent_snapshot, "
            "next_run_at, export_mode, schema_names) VALUES (:id, "
            ":name, :target_id, :schema_name, :directory, :enabled, "
            ":cron, :timezone, :retention_days, :parallel, "
            ":file_size_gb, :content, :consistent_snapshot, "
            ":next_run_at, :export_mode, :schema_names)"
        ),
        other.model_dump(mode="python"),
    )
    db.commit()
    return other


@pytest.mark.parametrize(
    "operation",
    [
        "enqueue",
        "queue_export",
        "cancel_export",
        "reconcile_export",
        "start_export",
        "monitor_export",
        "update_status",
        "expired_runs",
        "create_task",
        "update_task",
        "validate_target",
        "delete_export_files",
        "run",
    ],
)
def test_database_rejects_another_databases_tasks_and_exports(db, task, other_task, operation):
    """Wrong-owner operations fail before persistence changes or remote contact."""
    run = OracleDatabase(task.target_id, db).enqueue(task)
    run.state = "starting"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    db.commit()
    actor = next(
        iter(
            fetch_records(
                db,
                User,
                ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE role = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": "admin"}, row_limit=1),
            )
        ),
        None,
    )
    remote_calls = []

    @contextmanager
    def connection(dsn, credential):
        """Record unexpected remote access for the wrong database."""
        remote_calls.append(dsn)
        yield None

    database = OracleDatabase(other_task.target_id, db, connection_factory=connection)
    body = TaskInput(
        name=task.name,
        target_id=task.target_id,
        schema_name=task.schema_name,
        directory=task.directory,
    )
    args = {
        "enqueue": (task,),
        "queue_export": (task.id, actor),
        "cancel_export": (run.id, actor),
        "reconcile_export": (
            run.id,
            ReconcileInput(outcome="failed", reason="Investigated"),
            actor,
        ),
        "start_export": (run.id,),
        "monitor_export": (run.id,),
        "update_status": (run, JobStatus(state="COMPLETED", progress=100)),
        "expired_runs": (task,),
        "create_task": (body, actor),
        "update_task": (task.id, body, actor),
        "validate_target": (body,),
        "delete_export_files": (run.id, actor),
        "run": (run.id,),
    }
    with pytest.raises(ApplicationError, match="different database") as error:
        getattr(database, operation)(*args[operation])
    assert error.value.status_code == 409
    assert remote_calls == []
    assert run.state == "starting"
    assert not run.cancel_requested
    assert task.target_id != database.target_id
    assert (
        len(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"),
                {},
            )
        )
        == 1
    )
    assert (
        len(
            fetch_records(
                db,
                Task,
                ("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks"),
                {},
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    ("state", "age", "is_latest", "eligible"),
    [
        ("succeeded", 31, False, True),
        ("succeeded", 31, True, False),
        ("failed", 31, False, True),
        ("completed_with_errors", 31, False, True),
        ("reconciliation_required", 31, False, False),
        ("running", 31, False, False),
        ("cancelled", 30, False, False),
        ("failed", None, False, False),
    ],
)
def test_retention_uses_production_query_and_preserves_latest(db, task, state, age, is_latest, eligible):
    """Retention uses age and terminal state while preserving the latest success."""
    task.retention_days = 30
    execute(
        db,
        ("UPDATE tasks SET retention_days = :retention_days WHERE id = :record_key"),
        {"retention_days": task.retention_days, "record_key": task.id},
    )
    database = OracleDatabase(task.target_id, db, clock=lambda: CURRENT)
    run = database.enqueue(task)
    run.state = state
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    run.finished_at = CURRENT - timedelta(days=age) if age is not None else None
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    db.commit()
    if state == "succeeded" and not is_latest:
        latest = database.enqueue(task)
        latest.state = "succeeded"
        execute(
            db,
            ("UPDATE runs SET state = :state WHERE id = :record_key"),
            {"state": latest.state, "record_key": latest.id},
        )
        latest.finished_at = CURRENT
        execute(
            db,
            ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
            {"finished_at": latest.finished_at, "record_key": latest.id},
        )
        db.commit()
    assert (run.id in {candidate.id for candidate in database.expired_runs(task)}) is eligible


@pytest.mark.parametrize(
    ("directory", "filename"),
    [("OTHER", "oe_a7c92e_01.dmp"), ("DUMPS", "foreign.dmp")],
)
def test_database_manifest_rejects_wrong_directory_and_foreign_file(db, task, directory, filename):
    """Manual deletion and retention share captured manifest validation."""
    database = OracleDatabase(task.target_id, db, id_factory=lambda: "a7c92e" + "0" * 26)
    run = database.enqueue(task)
    run.snapshot = json.dumps({"directory": "DUMPS", "file_prefix": "oe_a7c92e"})
    execute(
        db,
        ("UPDATE runs SET snapshot = :snapshot WHERE id = :record_key"),
        {"snapshot": run.snapshot, "record_key": run.id},
        clobs=("snapshot",),
    )
    file = BackupFile(run_id=run.id, directory="DUMPS", filename="oe_a7c92e_01.dmp", kind="dump")
    database._validate_file(run, file)
    file.directory = directory
    execute(
        db,
        ("UPDATE backup_files SET directory = :directory WHERE id = :record_key"),
        {"directory": file.directory, "record_key": file.id},
    )
    file.filename = filename
    execute(
        db,
        ("UPDATE backup_files SET filename = :filename WHERE id = :record_key"),
        {"filename": file.filename, "record_key": file.id},
    )
    with pytest.raises(ValueError, match="namespace"):
        database._validate_file(run, file)


@pytest.mark.parametrize(("mode", "schemas"), [("SCHEMA", ["FINANCE"]), ("FULL", [])])
def test_database_enqueue_reads_legacy_and_full_scope(db, task, mode, schemas):
    """Schema normalization retains legacy single-schema and full export behavior."""
    task.export_mode = mode
    execute(
        db,
        ("UPDATE tasks SET export_mode = :export_mode WHERE id = :record_key"),
        {"export_mode": task.export_mode, "record_key": task.id},
    )
    task.schema_names = "[]"
    execute(
        db,
        ("UPDATE tasks SET schema_names = :schema_names WHERE id = :record_key"),
        {"schema_names": task.schema_names, "record_key": task.id},
        clobs=("schema_names",),
    )
    run = OracleDatabase(task.target_id, db).enqueue(task)
    assert json.loads(run.snapshot)["schema_names"] == schemas


def test_database_retention_visits_only_owned_tasks_and_exports(db, task, other_task):
    """Cleanup cannot delete another database's expired files."""
    own = OracleDatabase(task.target_id, db, clock=lambda: CURRENT).enqueue(task)
    foreign = OracleDatabase(other_task.target_id, db, clock=lambda: CURRENT).enqueue(other_task)
    for run in [own, foreign]:
        run.state = "failed"
        execute(
            db,
            ("UPDATE runs SET state = :state WHERE id = :record_key"),
            {"state": run.state, "record_key": run.id},
        )
        run.finished_at = CURRENT - timedelta(days=31)
        execute(
            db,
            ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
            {"finished_at": run.finished_at, "record_key": run.id},
        )
    db.commit()
    removed = []

    class Remote:
        """Record exact file deletion and accept master-table removal."""

        def remove_file(self, directory, filename):
            """Record one owned filename."""
            removed.append(filename)

        def release_master(self, job):
            """Accept remote master-table cleanup."""

    @contextmanager
    def connection(dsn, credential):
        """Yield the recording deletion adapter."""
        yield remote_connection(Remote())

    database = OracleDatabase(task.target_id, db, connection_factory=connection, clock=lambda: CURRENT)
    database.cleanup(lambda session: True)
    own_files = fetch_records(
        db,
        BackupFile,
        ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0"),
        {"p0": own.id},
    )
    other_files = fetch_records(
        db,
        BackupFile,
        ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0"),
        {"p0": foreign.id},
    )
    assert removed == [file.filename for file in own_files]
    assert all(file.deleted_at == CURRENT for file in own_files)
    assert all(file.deleted_at is None and file.cleanup_attempts == 0 for file in other_files)


def test_export_uses_captured_database_context_after_configuration_changes(factory, db, task):
    """Startup, monitoring, and deletion retain the queued DSN, credential, and directory."""
    run = OracleDatabase(task.target_id, db).enqueue(task)
    original = json.loads(run.snapshot)
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
    replacement = Credential(username="NEW_EXPORT", ciphertext=EncryptionSerivce.encrypt("new-test-password"))
    execute(
        db,
        ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
        replacement.model_dump(mode="python"),
        clobs=("ciphertext",),
    )

    target.dsn = "new-host:1521/new-service"
    execute(
        db,
        "UPDATE targets SET dsn = :dsn WHERE id = :record_key",
        {"dsn": target.dsn, "record_key": target.id},
    )
    target.export_credential_id = replacement.id
    execute(
        db,
        ("UPDATE targets SET export_credential_id = :export_credential_id WHERE id = :record_key"),
        {"export_credential_id": target.export_credential_id, "record_key": target.id},
    )
    task.directory = "NEW_DIRECTORY"
    execute(
        db,
        ("UPDATE tasks SET directory = :directory WHERE id = :record_key"),
        {"directory": task.directory, "record_key": task.id},
    )
    db.commit()
    assert Worker(factory).claim_run(db) == run.id
    connections = []
    deleted = []

    class Remote:
        """Assert captured export parameters across the lifecycle."""

        def start(self, job, run_id, options):
            """Start with the queued directory and task options."""
            assert options == original

        def status(self, job):
            """Return explicit completion evidence."""
            return JobStatus(state="COMPLETED", progress=100)

        def read_log(self, directory, filename):
            """Read the log from the captured directory."""
            assert directory == original["directory"]
            return []

        def file_size(self, directory, filename):
            """Measure files in the captured directory."""
            assert directory == original["directory"]
            return 42

        def verify_inactive(self, job):
            """Accept deletion of the completed remote export."""

        def remove_file(self, directory, filename):
            """Delete only captured export files."""
            assert directory == original["directory"]
            deleted.append(filename)

        def release_master(self, job):
            """Accept terminal master-table cleanup."""

    @contextmanager
    def connection(dsn, credential):
        """Record the selected captured endpoint and credential."""
        connections.append((dsn, credential.id))
        yield remote_connection(Remote())

    database = OracleDatabase(task.target_id, db, connection_factory=connection)
    database.start_export(run.id)
    assert database.monitor_export(run.id)
    actor = next(
        iter(
            fetch_records(
                db,
                User,
                ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE role = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": "admin"}, row_limit=1),
            )
        ),
        None,
    )
    assert actor is not None
    database.delete_export_files(run.id, actor)
    assert len(connections) == 4
    assert set(connections) == {(original["dsn"], original["credential_id"])}
    assert deleted == [original["file_prefix"] + ".log"]


def test_database_retention_excludes_exports_from_a_previously_owned_task(db, task, other_task):
    """Historical exports retain their owner even if old task records were reassigned."""
    original = OracleDatabase(task.target_id, db, clock=lambda: CURRENT)
    run = original.enqueue(task)
    run.state = "failed"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    run.finished_at = CURRENT - timedelta(days=31)
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    task.target_id = other_task.target_id
    execute(
        db,
        ("UPDATE tasks SET target_id = :target_id WHERE id = :record_key"),
        {"target_id": task.target_id, "record_key": task.id},
    )
    db.commit()
    current_owner = OracleDatabase(other_task.target_id, db, clock=lambda: CURRENT)
    assert current_owner.expired_runs(task) == []


def test_task_update_cannot_transfer_database_ownership(client, db, task, other_task):
    """The API rejects ownership changes before contacting another database."""
    from conftest import login

    login(client, "operator")
    response = client.put(
        f"/api/v1/tasks/{task.id}",
        json={
            "name": task.name,
            "target_id": other_task.target_id,
            "schema_name": task.schema_name,
            "directory": task.directory,
        },
    )
    assert response.status_code == 409
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Task,
                    ("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key"),
                    {"record_key": task.id},
                )
            ),
        ).target_id
        != other_task.target_id
    )


def test_database_history_pagination_excludes_other_targets(client, db, task, other_task):
    """Target pages and totals exclude foreign runs while global history includes them.

    Args:
        client (TestClient): FastAPI client backed by test metadata sessions.
        db (Connection): Metadata transaction for seeding exports.
        task (Task): Backup task owned by the requested database.
        other_task (Task): Backup task owned by a different database.
    """
    from conftest import login

    database = OracleDatabase(task.target_id, db, clock=lambda: CURRENT)
    older = database.enqueue(task)
    older.state = "succeeded"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": older.state, "record_key": older.id},
    )
    db.commit()
    database.clock = lambda: CURRENT + timedelta(minutes=1)
    newer = database.enqueue(task)
    foreign = OracleDatabase(other_task.target_id, db).enqueue(other_task)
    db.commit()
    login(client)

    url = f"/api/v1/targets/{task.target_id}/exports"
    first = client.get(url, params={"page_size": 1})
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert [item["id"] for item in first.json()["items"]] == [newer.id]
    second = client.get(url, params={"page_size": 1, "page": 2}).json()
    assert second["total"] == 2
    assert [item["id"] for item in second["items"]] == [older.id]
    assert client.get(url, params={"page_size": 1, "page": 3}).json()["items"] == []
    assert client.get(f"/api/v1/runs/{older.id}").json()["id"] == older.id
    history = client.get("/api/v1/runs").json()
    assert history["total"] == 3
    assert {item["id"] for item in history["items"]} == {older.id, newer.id, foreign.id}
    assert client.get(f"/api/v1/targets/{uid()}/exports").status_code == 404
    assert client.get(f"/api/v1/runs/{uid()}").status_code == 404
