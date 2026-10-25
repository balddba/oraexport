"""OracleDatabase export listing and manual file deletion regressions."""

import json
from contextlib import contextmanager

import pytest
from conftest import login
from dbapi import execute, fetch_records
from oracle_fakes import remote_connection

from oraexport.models.audit_event import AuditEvent
from oraexport.models.backup_file import BackupFile
from oraexport.models.defaults import now
from oraexport.models.run import Run
from oraexport.oracle_database import OracleDatabase


@pytest.fixture
def export(db, task):
    """Create a completed export with an owned dump and log.

    Args:
        db (Connection): Metadata database session fixture.
        task (Task): Seeded backup task record fixture.

    Returns:
        Run: Completed Run domain record with associated backup dump and log files.
    """
    # Enqueue a backup run and mark it as successfully completed
    run = OracleDatabase(task.target_id, db).enqueue(task)
    run.state = "succeeded"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    run.finished_at = now()
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    prefix = OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))
    execute(
        db,
        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
        BackupFile(run_id=run.id, directory=task.directory, kind="dump", size_bytes=1024, filename=prefix + "_01.dmp").model_dump(mode="python"),
        clobs=("cleanup_error",),
    )
    db.commit()
    return run


@pytest.fixture
def remote(monkeypatch):
    """Provide a recording Oracle adapter with controllable deletion failures.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.

    Returns:
        Oracle: Mock Oracle adapter tracking deleted files and simulating errors.
    """

    class Oracle:
        """Track exact remote calls without touching a real database."""

        def __init__(self) -> None:
            """Initialize tracking structures for remote calls."""
            self.removed = []
            self.fail_log = False
            self.active = False

        def verify_inactive(self, job):
            """Reject jobs still running in Oracle.

            Args:
                job (str): Oracle Data Pump job name.

            Raises:
                ValueError: If the mock job is flagged as active.
            """
            if self.active:
                raise ValueError("Oracle job still active")

        def remove_file(self, directory, filename):
            """Record a deletion or simulate a log-file failure.

            Args:
                directory (str): Oracle directory object name.
                filename (str): Name of the file being removed.

            Raises:
                RuntimeError: If fail_log is enabled and the file is a log file.
            """
            if self.fail_log and filename.endswith(".log"):
                raise RuntimeError("private connection details")
            self.removed.append((directory, filename))

    oracle = Oracle()

    @contextmanager
    def connection(*args):
        """Yield the recording target connection.

        Args:
            *args (Any): Connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(oracle)

    monkeypatch.setattr("oraexport.bootstrap.OracleUtilities.connect", connection)
    return oracle


def test_list_database_exports(client, db, export):
    """Paginate every tracked export and preserve manifests after deletion.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        export (Run): Completed export run fixture.
    """
    login(client, "viewer")
    endpoint = f"/api/v1/targets/{export.target_id}/exports"
    # Paginate with page_size=1
    response = client.get(endpoint + "?page_size=1")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == export.id
    assert len(data["items"][0]["files"]) == 2
    # Ensure sensitive credentials or DSN strings are redacted from response payload
    assert "credential_id" not in response.text
    assert "dsn" not in response.text
    # Empty second page
    assert client.get(endpoint + "?page=2&page_size=1").json()["items"] == []
    # Missing target ID returns 404
    assert client.get("/api/v1/targets/missing/exports").status_code == 404
    # Page size exceeding 100 limit returns 422
    assert client.get(endpoint + "?page_size=101").status_code == 422


def test_delete_latest_export_and_retry(client, db, export, remote):
    """Manual deletion overrides retention and is idempotent without losing history.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        export (Run): Completed export run fixture.
        remote (Oracle): Injected remote recording mock.
    """
    login(client, "operator")
    endpoint = f"/api/v1/runs/{export.id}/files"
    response = client.delete(endpoint)
    assert response.status_code == 200
    assert len(remote.removed) == 2
    assert all(file["deleted_at"] for file in response.json()["files"])
    assert response.json()["state"] == "succeeded"
    # Idempotent second deletion attempt succeeds without duplicating remote calls
    assert client.delete(endpoint).status_code == 200
    assert len(remote.removed) == 2
    # Verify audit event persistence
    db.rollback()
    assert next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                {"record_key": export.id},
            )
        ),
        None,
    )
    assert next(
        iter(
            fetch_records(
                db,
                AuditEvent,
                ("SELECT id, user_id, action, resource_id, created_at FROM audit_events WHERE action = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": "export_files_deleted"}, row_limit=1),
            )
        ),
        None,
    )


@pytest.mark.parametrize("state", ["queued", "starting", "running", "reconciliation_required"])
def test_reject_busy_export(client, db, export, remote, state):
    """Never remove files needed by a pending or uncertain job.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        export (Run): Export run fixture.
        remote (Oracle): Injected remote recording mock.
        state (str): Active or uncertain non-terminal state name.
    """
    export.state = state
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": export.state, "record_key": export.id},
    )
    db.commit()
    login(client)
    # Active runs cannot have their files deleted
    assert client.delete(f"/api/v1/runs/{export.id}/files").status_code == 409
    assert not remote.removed


def test_delete_permissions_and_missing(client, export, remote):
    """Enforce authentication and operator permissions at the API boundary.

    Args:
        client (TestClient): FastAPI HTTP test client.
        export (Run): Export run fixture.
        remote (Oracle): Injected remote recording mock.
    """
    endpoint = f"/api/v1/runs/{export.id}/files"
    # Unauthenticated request returns 401
    assert client.delete(endpoint).status_code == 401
    # Viewer role lacks operator permission and returns 403
    login(client, "viewer")
    assert client.delete(endpoint).status_code == 403
    # Non-existent run ID returns 404
    login(client)
    assert client.delete("/api/v1/runs/missing/files").status_code == 404
    assert not remote.removed


@pytest.mark.parametrize("corruption", ["filename", "directory"])
def test_delete_rejects_unowned_manifest(client, db, export, remote, corruption):
    """Validate all filenames and directories before deleting any file.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        export (Run): Export run fixture.
        remote (Oracle): Injected remote recording mock.
        corruption (str): Attribute name to corrupt ('filename' or 'directory').
    """
    file = next(
        iter(
            fetch_records(
                db,
                BackupFile,
                ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0 AND kind = :p1 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": export.id, "p1": "dump"}, row_limit=1),
            )
        ),
        None,
    )
    assert file is not None
    setattr(file, corruption, "OTHER")
    execute(
        db,
        ("UPDATE backup_files SET filename = :filename, directory = :directory WHERE id = :id"),
        {"filename": file.filename, "directory": file.directory, "id": file.id},
    )
    db.commit()
    login(client)
    # Manifest validation rejection returns 409 Conflict
    assert client.delete(f"/api/v1/runs/{export.id}/files").status_code == 409
    assert not remote.removed


def test_partial_deletion_retry(client, export, remote):
    """Persist successes and sanitized errors, then retry only remaining files.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        export (Run): Export run fixture.
        remote (Oracle): Injected remote recording mock.
    """
    login(client)
    endpoint = f"/api/v1/runs/{export.id}/files"
    remote.fail_log = True
    # Initial deletion fails on log file with 502 Bad Gateway
    assert client.delete(endpoint).status_code == 502
    files = client.get(f"/api/v1/runs/{export.id}").json()["files"]
    # Dump file succeeded, log file recorded sanitized error
    assert next(file for file in files if file["kind"] == "dump")["deleted_at"]
    log = next(file for file in files if file["kind"] == "log")
    assert log["cleanup_error"] == "RuntimeError"
    assert not log["deleted_at"]
    # Retry after clearing failure completes log deletion
    remote.fail_log = False
    assert client.delete(endpoint).status_code == 200
    assert len(remote.removed) == 2


def test_delete_verifies_remote_inactivity(client, export, remote):
    """Refuse deletion if terminal metadata disagrees with remote job state.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        export (Run): Export run fixture.
        remote (Oracle): Injected remote recording mock.
    """
    login(client)
    remote.active = True
    # If Oracle reports job is still active, deletion is refused
    assert client.delete(f"/api/v1/runs/{export.id}/files").status_code == 502
    assert not remote.removed


def test_delete_connection_failure(client, export, remote, monkeypatch):
    """Keep every file retained when the target cannot be reached.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        export (Run): Export run fixture.
        remote (Oracle): Injected remote recording mock.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """

    @contextmanager
    def unavailable(*args):
        """Simulate a target connection failure.

        Args:
            *args (Any): Connection parameters.

        Raises:
            RuntimeError: Simulating target connection drop.

        Yields:
            None: Never yields due to simulated exception.
        """
        raise RuntimeError("private target credentials")
        yield

    monkeypatch.setattr("oraexport.bootstrap.OracleUtilities.connect", unavailable)
    login(client)
    response = client.delete(f"/api/v1/runs/{export.id}/files")
    assert response.status_code == 502
    # Ensure sensitive target credentials are not reflected in the error response
    assert "private" not in response.text
    files = client.get(f"/api/v1/runs/{export.id}").json()["files"]
    assert all(not file["deleted_at"] and file["cleanup_error"] == "RuntimeError" for file in files)
    assert not remote.removed
