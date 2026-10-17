"""Durable scheduling, recovery, cleanup, and outbox regression tests."""

import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from dbapi import execute, fetch_records
from oracle_fakes import remote_connection

from oraexport.models.backup_file import BackupFile
from oraexport.models.defaults import now
from oraexport.models.delivery import Delivery
from oraexport.models.event_details import EventDetails
from oraexport.models.job_status import JobStatus
from oraexport.models.lease import Lease
from oraexport.models.notification_settings import NotificationSettings
from oraexport.models.run import Run
from oraexport.models.task import Task
from oraexport.notifications import NotificationService
from oraexport.oracle_database import OracleDatabase
from oraexport.worker import Worker


class FakeOracle:
    """Remote transport substitute with explicit observable side effects.

    Records remote Data Pump API calls and simulates network faults, status responses,
    and file deletions for worker integration testing.
    """

    def __init__(self):
        """Initialize a controllable status and call recorder."""
        self.calls = []
        self.result = JobStatus(state="EXECUTING", progress=40)
        self.broken = False

    def start(self, job, run_id, options):
        """Record a startup or simulate an uncertain network failure.

        Args:
            job (str): Oracle Data Pump job name.
            run_id (str): Unique run identifier.
            options (dict[str, Any]): Export configuration options dictionary.

        Raises:
            ConnectionError: If simulated broken connection flag is set.
        """
        self.calls.append(("start", job))
        if self.broken:
            raise ConnectionError("password=secret must not escape")

    def status(self, job):
        """Return status or a lost connection.

        Args:
            job (str): Oracle Data Pump job name.

        Returns:
            JobStatus: Configured status response.

        Raises:
            ConnectionError: If simulated broken connection flag is set.
        """
        if self.broken:
            raise ConnectionError("password=secret")
        return self.result

    def cancel(self, job):
        """Simulate an explicitly stopped job.

        Args:
            job (str): Oracle Data Pump job name.
        """
        self.calls.append(("cancel", job))
        self.result = JobStatus(state="STOPPED")

    def read_log(self, directory, filename):
        """Return no completion marker for nonterminal jobs.

        Args:
            directory (str): Oracle directory object name.
            filename (str): Log filename.

        Returns:
            list[str]: Empty list of completion log lines.
        """
        return []

    def file_size(self, directory, filename):
        """Measure a known file deterministically.

        Args:
            directory (str): Oracle directory object name.
            filename (str): Dump or log filename.

        Returns:
            int: Deterministic file size in bytes.
        """
        return 1234

    def release_master(self, job):
        """Simulate safe removal of the owned master table.

        Args:
            job (str): Oracle Data Pump job name.
        """

    def remove_file(self, directory, filename):
        """Record exact-file removal.

        Args:
            directory (str): Oracle directory object name.
            filename (str): Target filename to delete.
        """
        self.calls.append(("remove", filename))


@pytest.fixture
def oracle(monkeypatch):
    """Replace only the worker transport boundary.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.

    Returns:
        FakeOracle: Configured FakeOracle test double instance.
    """
    import oraexport.worker as module

    remote = FakeOracle()

    @contextmanager
    def connection(*args):
        """Yield the fake remote transport.

        Args:
            *args (Any): Connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(remote)

    monkeypatch.setattr(module.OracleUtilities, "connect", connection)
    return remote


def test_lease_excludes_other_worker(factory, db):
    """Only the current lease owner can dispatch or monitor work.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
    """
    # Decompose worker instantiations into single lines
    first = Worker(factory)
    second = Worker(factory)
    # First worker acquires distributed scheduler lease
    assert first.acquire_or_renew_lease(db)
    # Competing worker cannot acquire active lease
    assert not second.acquire_or_renew_lease(db)
    # Expire lease to verify automatic handover
    lease = next(
        iter(
            fetch_records(
                db,
                Lease,
                ("SELECT name, owner, expires_at, last_cleanup_at FROM worker_leases WHERE name = :record_key"),
                {"record_key": "scheduler"},
            )
        ),
        None,
    )
    assert lease is not None
    lease.expires_at = now() - timedelta(seconds=1)
    execute(
        db,
        ("UPDATE worker_leases SET expires_at = :expires_at WHERE name = :record_key"),
        {"expires_at": lease.expires_at, "record_key": lease.name},
    )
    db.commit()
    assert second.acquire_or_renew_lease(db)


def test_schedules_coalesce_and_do_not_duplicate(factory, db, task):
    """An outage generates one pending job and skips stale occurrences.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
    # Simulate overdue task from 40 days in the past
    task.next_run_at = now() - timedelta(days=40)
    execute(
        db,
        ("UPDATE tasks SET next_run_at = :next_run_at WHERE id = :record_key"),
        {"next_run_at": task.next_run_at, "record_key": task.id},
    )
    db.commit()
    worker = Worker(factory)
    worker.scheduler.schedule(db)
    # Exactly one run queued; next_run_at advanced into the future
    assert (
        len(
            list(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"),
                    {},
                )
            )
        )
        == 1
    )
    task = next(
        iter(
            fetch_records(
                db,
                Task,
                ("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key"),
                {"record_key": task.id},
            )
        ),
        task,
    )
    assert task.next_run_at is not None
    assert task.next_run_at > now()
    # Second scheduler tick does not create duplicate run
    worker.scheduler.schedule(db)
    assert (
        len(
            list(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"),
                    {},
                )
            )
        )
        == 1
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_schedule_uses_oracle_compatible_boolean_filter(factory, db, task, enabled):
    """Oracle stores booleans numerically and rejects the invalid IS 1 syntax.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        enabled (bool): Boolean toggle for task.enabled flag.
    """
    task.enabled = enabled
    execute(
        db,
        ("UPDATE tasks SET enabled = :enabled WHERE id = :record_key"),
        {"enabled": task.enabled, "record_key": task.id},
    )
    due_at = now() - timedelta(days=1)
    task.next_run_at = due_at
    execute(
        db,
        ("UPDATE tasks SET next_run_at = :next_run_at WHERE id = :record_key"),
        {"next_run_at": task.next_run_at, "record_key": task.id},
    )
    db.commit()
    db.statements.clear()
    Worker(factory).scheduler.schedule(db)
    # Inspect generated SQL: must use numeric equality (= 1) rather than IS 1
    query = next(stmt[0] for stmt in db.statements if "FROM tasks" in stmt[0])
    assert "enabled = 1" in query
    assert "enabled IS" not in query
    assert "FOR UPDATE" in query
    assert len(
        list(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"),
                {},
            )
        )
    ) == int(enabled)
    task = next(
        iter(
            fetch_records(
                db,
                Task,
                ("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key"),
                {"record_key": task.id},
            )
        ),
        task,
    )
    assert task.next_run_at is not None
    assert task.next_run_at > now() if enabled else task.next_run_at == due_at


def test_start_once_recover_and_cancel(factory, db, task, oracle):
    """A fresh worker monitors persisted jobs without invoking START_JOB again.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    worker = Worker(factory)
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
    assert worker.claim_run(db) == run.id
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
    assert run.state == "starting"
    worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).start_export(run.id)
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
    assert run.state == "running"
    # Separate recovery worker instance picks up active execution
    recovered = Worker(factory)
    recovered.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).monitor_export(run.id)
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
    assert run.progress == 40
    assert [call[0] for call in oracle.calls] == ["start"]
    # Request cancellation and verify terminal transition
    run.cancel_requested = True
    execute(
        db,
        ("UPDATE runs SET cancel_requested = :cancel_requested WHERE id = :record_key"),
        {"cancel_requested": run.cancel_requested, "record_key": run.id},
    )
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
    assert recovered.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).monitor_export(run.id)
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
    assert run.state == "cancelled"
    assert run.finished_at


def test_uncertain_start_is_not_retried(factory, db, task, oracle):
    """Connection failures reserve the active slot until the outcome is reconciled.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    worker = Worker(factory)
    assert worker.claim_run(db)
    oracle.broken = True
    # Startup connection failure marks run as reconciliation_required
    worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).start_export(run.id)
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
    assert run.state == "reconciliation_required"
    worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).monitor_export(run.id)
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
    assert run.state == "reconciliation_required"
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
    assert "secret" not in run.errors + run.messages
    # Reconciliation required runs block claiming new work on the target
    assert worker.claim_run(db) is None
    assert len(oracle.calls) == 1


def test_concurrency_per_target(factory, db, task):
    """Active work on a target prevents a second task there from being claimed.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
    first = OracleDatabase(task.target_id, db).enqueue(task)
    second_task = Task(name="Second", target_id=task.target_id, schema_name="HR", directory="EXPORT_DIR")
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
        second_task.model_dump(mode="python"),
        clobs=("schema_names",),
    )

    second = OracleDatabase(second_task.target_id, db).enqueue(second_task)
    db.commit()
    worker = Worker(factory)
    # First task on target is claimed; second task is throttled
    first = next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                {"record_key": first.id},
            )
        ),
        first,
    )
    assert worker.claim_run(db) == first.id
    assert worker.claim_run(db) is None
    second = next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                {"record_key": second.id},
            )
        ),
        second,
    )
    assert second.state == "queued"


def test_terminal_sizes_and_cleanup(factory, db, task, oracle):
    """Persist dump sizes and remove only expired owned files; keep last success.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    worker = Worker(factory)
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
    assert worker.claim_run(db) == run.id
    prefix = OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))
    oracle.result = JobStatus(state="COMPLETED", progress=100, files=[f"/data/{prefix}_01.dmp"])
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
    assert worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).monitor_export(run.id)
    files = list(
        fetch_records(
            db,
            BackupFile,
            ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0"),
            {"p0": run.id},
        )
    )
    assert all(file.size_bytes == 1234 for file in files)
    run.finished_at = now() - timedelta(days=100)
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    db.commit()
    # Most recent success is preserved despite age
    worker.cleanup(db)
    assert not any(call[0] == "remove" for call in oracle.calls)
    # Failed run is eligible for retention cleanup
    run.state = "failed"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    db.commit()
    worker.cleanup(db)
    files = fetch_records(
        db,
        BackupFile,
        ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :run_id"),
        {"run_id": run.id},
    )
    assert all(file.deleted_at for file in files)
    assert {call[1] for call in oracle.calls if call[0] == "remove"} == {file.filename for file in files}


def test_cleanup_retries_partial_failure(factory, db, task, oracle, monkeypatch):
    """One removal failure does not prevent other files from being cleaned up.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Decompose multi-variable assignment into single lines
    run.state = "failed"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    run.finished_at = now() - timedelta(days=100)
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    prefix = OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))
    execute(
        db,
        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
        BackupFile(
            run_id=run.id,
            directory="EXPORT_DIR",
            filename=f"{prefix}_01.dmp",
            kind="dump",
        ).model_dump(mode="python"),
        clobs=("cleanup_error",),
    )
    db.commit()
    original = oracle.remove_file

    def fail_log(directory, filename):
        """Simulate one inaccessible log file.

        Args:
            directory (str): Oracle directory object name.
            filename (str): Filename being removed.

        Raises:
            RuntimeError: Simulating access denial on log files.
        """
        if filename.endswith(".log"):
            raise RuntimeError("private connection details")
        original(directory, filename)

    monkeypatch.setattr(oracle, "remove_file", fail_log)
    worker = Worker(factory)
    worker.cleanup(db)
    files = list(
        fetch_records(
            db,
            BackupFile,
            ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0"),
            {"p0": run.id},
        )
    )
    # Partial success: dump file deleted, log file cleanup error recorded
    assert sum(file.deleted_at is not None for file in files) == 1
    assert next(file for file in files if file.kind == "log").cleanup_error == "RuntimeError"
    # Restoring permission completes the cleanup
    monkeypatch.setattr(oracle, "remove_file", original)
    worker.cleanup(db)
    files = fetch_records(
        db,
        BackupFile,
        ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :run_id"),
        {"run_id": run.id},
    )
    assert all(file.deleted_at for file in files)


def test_notification_retries_bounded(db, monkeypatch):
    """Failed deliveries retry five times without altering export records.

    Args:
        db (Connection): Metadata database session.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    execute(
        db,
        ("INSERT INTO notification_settings (id, config, secrets) VALUES (:id, :config, :secrets)"),
        NotificationSettings(id=1, config=json.dumps({"webhook_url": "https://example/event"})).model_dump(mode="python"),
        clobs=("config", "secrets"),
    )
    db.commit()
    NotificationService().queue_event(db=db, event="export_failed", details=EventDetails(root={"run_id": "test"}))
    db.commit()

    def fail(*args):
        """Simulate a delivery failure containing a secret-bearing URL.

        Args:
            *args (Any): Sender arguments.

        Raises:
            RuntimeError: Simulated network failure with redacted secret URL.
        """
        raise RuntimeError("https://secret:password@example/event")

    dispatcher = NotificationService(sender=fail)
    # Retry delivery 5 times with exponential backoff simulation
    for _ in range(5):
        entry = next(
            iter(
                fetch_records(
                    db,
                    Delivery,
                    ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries FETCH NEXT :row_limit ROWS ONLY"),
                    dict({} or {}, row_limit=1),
                )
            ),
            None,
        )
        assert entry is not None
        entry.next_attempt_at = now() - timedelta(seconds=1)
        execute(
            db,
            ("UPDATE notification_deliveries SET next_attempt_at = :next_attempt_at WHERE id = :record_key"),
            {"next_attempt_at": entry.next_attempt_at, "record_key": entry.id},
        )
        db.commit()
        dispatcher.process_outbox(db)
    entry = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": entry.id},
            )
        ),
        entry,
    )
    assert entry.attempts == 5
    assert entry.state == "failed"
    entry = next(
        iter(
            fetch_records(
                db,
                Delivery,
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE id = :record_key"),
                {"record_key": entry.id},
            )
        ),
        entry,
    )
    assert entry.error == "RuntimeError"


def test_cancel_between_claim_and_start(factory, db, task, oracle):
    """Cancellation recorded after a queue claim can prevent remote startup entirely.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    worker = Worker(factory)
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
    assert worker.claim_run(db) == run.id
    # Intervening cancel request before worker executes start
    run.cancel_requested = True
    execute(
        db,
        ("UPDATE runs SET cancel_requested = :cancel_requested WHERE id = :record_key"),
        {"cancel_requested": run.cancel_requested, "record_key": run.id},
    )
    db.commit()
    worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).start_export(run.id)
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
    assert run.state == "cancelled"
    assert not oracle.calls


def test_monitor_never_overwrites_admin_acknowledgment(factory, db, task, oracle):
    """A terminal state committed by another session must survive stale monitor lists.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Decompose multi-variable assignment into single lines
    run.state = "failed"
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
    db.commit()
    # Stale monitor list invocation returns immediately without overriding terminal state
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
    assert (
        not Worker(factory)
        .database(
            db,
            next(
                iter(
                    fetch_records(
                        db,
                        Run,
                        ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                        {"record_key": run.id},
                    )
                ),
            ).target_id,
        )
        .monitor_export(run.id)
    )
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
    assert run.state == "failed"


def test_cleanup_refuses_corrupt_manifest(factory, db, task, oracle):
    """Exact filename and directory checks reject traversal or misplaced artifacts.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Decompose multi-variable assignment into single lines
    run.state = "failed"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    run.finished_at = now() - timedelta(days=100)
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    prefix = OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))
    execute(
        db,
        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
        BackupFile(
            run_id=run.id,
            directory="OTHER_DIR",
            filename=f"{prefix}_01.dmp",
            kind="dump",
        ).model_dump(mode="python"),
        clobs=("cleanup_error",),
    )
    execute(
        db,
        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
        BackupFile(
            run_id=run.id,
            directory="EXPORT_DIR",
            filename=f"../{prefix}_02.dmp",
            kind="dump",
        ).model_dump(mode="python"),
        clobs=("cleanup_error",),
    )
    db.commit()
    worker = Worker(factory)
    worker.cleanup(db)
    # Traversal and foreign directory files must be rejected during cleanup
    removed = [call[1] for call in oracle.calls if call[0] == "remove"]
    assert removed == [f"{prefix}.log"]


def test_export_errors_queue_one_failure_notification(factory, db, task, oracle):
    """Completed-with-errors exports notify once and retain their export outcome.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        oracle (FakeOracle): Injected remote transport test double.
    """
    execute(
        db,
        ("INSERT INTO notification_settings (id, config, secrets) VALUES (:id, :config, :secrets)"),
        NotificationSettings(id=1, config=json.dumps({"webhook_url": "https://example/event"})).model_dump(mode="python"),
        clobs=("config", "secrets"),
    )
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    worker = Worker(factory)
    assert worker.claim_run(db)
    # Simulate completed job with warnings/errors
    oracle.result = JobStatus(state="COMPLETED", progress=100, error_count=1, errors=["ORA-39166: missing object"])
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
    assert worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).monitor_export(run.id)
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
    assert run.state == "completed_with_errors"
    # Exactly one delivery event queued in outbox
    entries = list(
        fetch_records(
            db,
            Delivery,
            ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries"),
            {},
        )
    )
    assert len(entries) == 1
    assert json.loads(entries[0].payload)["event"] == "export_completed_with_errors"
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
    assert not worker.database(
        db,
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).target_id,
    ).monitor_export(run.id)
    assert (
        len(
            list(
                fetch_records(
                    db,
                    Delivery,
                    ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries"),
                    {},
                )
            )
        )
        == 1
    )


@pytest.mark.parametrize("files_deleted", [False, True])
def test_master_cleanup_retries_latest_success_without_retained_files(factory, db, task, oracle, monkeypatch, files_deleted):
    """Retry durable cleanup independently of retention and stop after success.

    Args:
        factory (Callable): Test metadata factory.
        db (Connection): Metadata transaction.
        task (Task): Seeded policy.
        oracle (FakeOracle): Controlled remote transport.
        monkeypatch (pytest.MonkeyPatch): Remote failure injection.
        files_deleted (bool): Whether the manifest has already been deleted.
    """
    calls = []

    def release(database, job):
        """Fail the initial drop and allow subsequent cleanup to succeed."""
        calls.append(job)
        if len(calls) == 1:
            raise ConnectionError("temporary failure")

    monkeypatch.setattr(OracleDatabase, "release_datapump_master", release)
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    worker = Worker(factory)
    worker.claim_run(db)
    worker.database(db, task.target_id).start_export(run.id)
    oracle.result = JobStatus(state="COMPLETED", progress=100)
    assert worker.database(db, task.target_id).monitor_export(run.id)
    saved = fetch_records(db, Run, "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :id", {"id": run.id})[0]
    assert saved.state == "succeeded"
    assert saved.master_cleanup_pending
    if files_deleted:
        execute(
            db,
            "UPDATE backup_files SET deleted_at = :deleted_at WHERE run_id = :run_id",
            {"deleted_at": now(), "run_id": run.id},
        )
        db.commit()
    worker.cleanup(db)
    saved = fetch_records(db, Run, "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :id", {"id": run.id})[0]
    assert saved.master_cleanup_pending is False
    assert len(calls) == 2
    worker.cleanup(db)
    assert len(calls) == 2
    assert not any(call[0] == "remove" for call in oracle.calls)


def test_master_cleanup_keeps_failed_intent_and_deduplicates_errors(factory, db, task, oracle, monkeypatch):
    """Repeated cleanup failures retain their durable intent without growing messages.

    Args:
        factory (Callable): Metadata connection factory.
        db (Connection): Metadata transaction.
        task (Task): Seeded task.
        oracle (FakeOracle): Fake remote transport.
        monkeypatch (pytest.MonkeyPatch): Remote failure injection.
    """

    def release(database, job):
        """Keep the master table unavailable during retry."""
        raise ConnectionError("temporary failure")

    monkeypatch.setattr(OracleDatabase, "release_datapump_master", release)
    run = OracleDatabase(task.target_id, db).enqueue(task)
    execute(
        db,
        "UPDATE runs SET state = 'failed', finished_at = :at WHERE id = :id",
        {"at": now(), "id": run.id},
    )
    db.commit()
    worker = Worker(factory)
    worker.cleanup(db)
    worker.cleanup(db)
    saved = fetch_records(db, Run, "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :id", {"id": run.id})[0]
    assert saved.master_cleanup_pending
    assert len([message for message in json.loads(saved.messages) if message.startswith("Master-table cleanup deferred:")]) == 1
