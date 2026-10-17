"""Readable naming, immutable namespaces, and compatibility regression tests."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from dbapi import execute, fetch_records
from oracle_fakes import remote_connection

from oraexport.models.backup_file import BackupFile
from oraexport.models.defaults import now
from oraexport.models.registration import Registration
from oraexport.models.run import Run
from oraexport.models.target import Target
from oraexport.oracle_database import OracleDatabase
from oraexport.worker import Worker

RUN_ID = "a7c92e12345678901234567890123456"
CREATED = datetime(2026, 10, 12, 14, 30, 25)


@pytest.mark.parametrize(
    ("pdb", "schemas", "mode", "expected"),
    [
        ("SALES", ["HR"], "SCHEMA", "PRODDB_SALES_HR"),
        ("SALES", ["HR", "PAYROLL"], "SCHEMA", "PRODDB_SALES_HR+PAYROLL"),
        ("SALES", ["HR", "PAYROLL", "A", "B", "C"], "SCHEMA", "PRODDB_SALES_HR+PAYROLL+3more"),
        ("SALES", [], "FULL", "PRODDB_SALES_FULL"),
        (None, ["HR"], "SCHEMA", "PRODDB_HR"),
    ],
)
def test_readable_names(pdb, schemas, mode, expected):
    """Include only appropriate topology and scope components in date-first names.

    Args:
        pdb (str | None): Container/PDB name or None for non-CDB.
        schemas (list[str]): List of schema names selected for export.
        mode (str): Export mode ('SCHEMA' or 'FULL').
        expected (str): Expected readable name segment.
    """
    # Generate human-readable date-prefixed file namespace
    prefix = OracleDatabase._readable_prefix(CREATED, "PRODDB", pdb, schemas, mode, RUN_ID)
    assert prefix == f"2026-10-12_143025_{expected}_a7c92e"
    assert OracleDatabase.file_prefix(RUN_ID, {"file_prefix": prefix}) == prefix


def test_safe_bounded_names():
    """Sanitize paths and templates while bounding even maximal schema selections."""
    # Sanitize special characters and traversal patterns into safe hyphenated tokens
    bad_name = "../../Prod DB%L"
    prefix = OracleDatabase._readable_prefix(CREATED, bad_name, "SALES/PDB", ["APP_OWNER"], "SCHEMA", RUN_ID)
    assert "Prod-DB-L_SALES-PDB_APP_OWNER" in prefix
    assert not any(character in prefix for character in "/\\.% ")
    # Bounded prefix generation ensures file paths stay well below Oracle 255-character limit
    prefix = OracleDatabase._readable_prefix(CREATED, "D" * 128, "P" * 128, ["S" * 128] * 200, "SCHEMA", RUN_ID, 32)
    assert len(prefix + "_2147483646.dmp") < 255
    assert OracleDatabase.file_prefix(RUN_ID, {"file_prefix": prefix}) == prefix


@pytest.mark.parametrize("prefix", ["../foreign_a7c92e", "bad%L_a7c92e", "other_abcdef", 123])
def test_reject_invalid_saved_prefix(prefix):
    """Refuse unsafe or unrelated saved namespaces before using them remotely.

    Args:
        prefix (Any): Unsafe or malformed filename prefix under test.
    """
    # Prevent path traversal or format string attacks in saved prefix options
    with pytest.raises(ValueError, match="prefix"):
        OracleDatabase.file_prefix(RUN_ID, {"file_prefix": prefix})


def test_queue_names_are_immutable_and_collisions_extend_id(db, task, monkeypatch):
    """Save topology/time at enqueue and lengthen only a colliding short identifier.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    # Decompose multi-variable assignments into single lines
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
    registration = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": target.registration_id},
            )
        ),
        None,
    )
    assert registration is not None
    registration.name = "PRODDB"
    execute(
        db,
        ("UPDATE registrations SET name = :name WHERE id = :record_key"),
        {"name": registration.name, "record_key": registration.id},
    )
    registration.kind = "cdb"
    execute(
        db,
        ("UPDATE registrations SET kind = :kind WHERE id = :record_key"),
        {"kind": registration.kind, "record_key": registration.id},
    )
    target.container_name = "SALES"
    execute(
        db,
        ("UPDATE targets SET container_name = :container_name WHERE id = :record_key"),
        {"container_name": target.container_name, "record_key": target.id},
    )
    # Seed an earlier run with a conflicting prefix
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
            id="previous",
            task_id=task.id,
            target_id=task.target_id,
            occurrence="previous",
            snapshot="{}",
            oracle_job="OE_PREVIOUS",
            state="failed",
        ).model_dump(mode="python"),
        clobs=("snapshot", "errors", "messages"),
    )

    execute(
        db,
        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
        BackupFile(
            run_id="previous",
            directory=task.directory,
            filename="2026-10-12_143025_PRODDB_SALES_FINANCE_a7c92e.log",
            kind="log",
        ).model_dump(mode="python"),
        clobs=("cleanup_error",),
    )
    db.commit()
    # Enqueue a new run; colliding short hash must automatically extend to 8 chars
    run = OracleDatabase(task.target_id, db, clock=lambda: CREATED, id_factory=lambda: RUN_ID).enqueue(task)
    db.commit()
    prefix = OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))
    assert prefix == "2026-10-12_143025_PRODDB_SALES_FINANCE_a7c92e12"
    # Modifying upstream registration or schema later must not mutate immutable queued snapshot
    registration.name = "RENAMED"
    execute(
        db,
        ("UPDATE registrations SET name = :name WHERE id = :record_key"),
        {"name": registration.name, "record_key": registration.id},
    )
    target.container_name = "OTHER"
    execute(
        db,
        ("UPDATE targets SET container_name = :container_name WHERE id = :record_key"),
        {"container_name": target.container_name, "record_key": target.id},
    )
    task.schema_name = "HR"
    execute(
        db,
        ("UPDATE tasks SET schema_name = :schema_name WHERE id = :record_key"),
        {"schema_name": task.schema_name, "record_key": task.id},
    )
    db.commit()
    assert OracleDatabase.file_prefix(run.id, json.loads(run.snapshot)) == prefix
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    BackupFile,
                    ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                    dict({"p0": run.id}, row_limit=1),
                )
            ),
        ).filename
        == prefix + ".log"
    )


def test_new_namespace_ownership_and_log_recovery():
    """Recognize exact dump pieces on both path styles and reject foreign namespaces."""
    prefix = OracleDatabase._readable_prefix(CREATED, "PRODDB", "SALES", ["HR", "PAYROLL"], "SCHEMA", RUN_ID)
    options = {"file_prefix": prefix}
    dump = prefix + "_01.dmp"
    # Verify recognition of dump files matching the namespace across Unix and Windows paths
    assert OracleDatabase.owned_filename(RUN_ID, "/exports/" + dump, options) == dump
    assert OracleDatabase.owned_filename(RUN_ID, "C:\\exports\\" + dump, options) == dump
    # Disallow partial, corrupt, foreign, or unowned filenames
    for foreign in [
        prefix + "_%L.dmp",
        prefix + "_01.dmp.bak",
        "x" + dump,
        f"oe_{RUN_ID}_01.dmp",
        dump.replace("a7c92e", "abcdef"),
    ]:
        assert OracleDatabase.owned_filename(RUN_ID, foreign, options) is None
    # Decompose multi-line log list into single-line entries
    log_lines = ["/exports/" + dump, "x" + dump, dump + ".bak"]
    log_lines.append('Job "ORAEXPORT"."OE_JOB" successfully completed')
    recovered = OracleDatabase.recover_from_log("OE_JOB", RUN_ID, log_lines, options)
    assert recovered is not None
    assert recovered.files == [dump]


@pytest.mark.parametrize(
    ("kind", "container", "include_pdb"),
    [
        ("cdb", "SALES", True),
        ("pdb", "SALES", True),
        ("non_cdb", "SALES", False),
        ("unknown", None, False),
    ],
)
def test_queue_includes_pdb_only_when_appropriate(db, task, kind, container, include_pdb):
    """Non-CDB and undiscovered targets omit the PDB component of queued names.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        kind (str): OracleDatabase architecture kind ('cdb', 'pdb', 'non_cdb', 'unknown').
        container (str | None): Container name or None.
        include_pdb (bool): Whether PDB segment should appear in filename.
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
    registration = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": target.registration_id},
            )
        ),
        None,
    )
    assert registration is not None
    # Decompose multi-variable assignment into single lines
    registration.kind = kind
    execute(
        db,
        ("UPDATE registrations SET kind = :kind WHERE id = :record_key"),
        {"kind": registration.kind, "record_key": registration.id},
    )
    target.container_name = container
    execute(
        db,
        ("UPDATE targets SET container_name = :container_name WHERE id = :record_key"),
        {"container_name": target.container_name, "record_key": target.id},
    )
    run = OracleDatabase(task.target_id, db).enqueue(task)
    options = json.loads(run.snapshot)
    assert options["pdb_name"] == (container if include_pdb else None)
    assert ("_SALES_" in options["file_prefix"]) == include_pdb


@pytest.mark.parametrize("legacy", [True, False])
def test_saved_run_recovery_and_cleanup(factory, db, task, monkeypatch, legacy):
    """Both naming formats recover their saved log and clean up their exact files.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
        legacy (bool): Whether to test legacy filename format without prefix field.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    options = json.loads(run.snapshot)
    if legacy:
        del options["file_prefix"]
    run.snapshot = json.dumps(options)
    execute(
        db,
        ("UPDATE runs SET snapshot = :snapshot WHERE id = :record_key"),
        {"snapshot": run.snapshot, "record_key": run.id},
        clobs=("snapshot",),
    )
    prefix = OracleDatabase.file_prefix(run.id, options)
    run.state = "running"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    selected_record = next(
        iter(
            fetch_records(
                db,
                BackupFile,
                ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": run.id}, row_limit=1),
            )
        ),
        None,
    )
    assert selected_record is not None
    selected_record.filename = prefix + ".log"
    execute(
        db,
        ("UPDATE backup_files SET filename = :filename WHERE id = :record_key"),
        {"filename": selected_record.filename, "record_key": selected_record.id},
    )
    db.commit()
    calls = []

    class Remote:
        """Recover an absent legacy job from its explicit completion log."""

        def status(self, job):
            """Simulate a job that has already detached.

            Args:
                job (str): Oracle Data Pump job name.

            Raises:
                RuntimeError: Simulating an absent or detached job.
            """
            raise RuntimeError("Job absent")

        def read_log(self, directory, filename):
            """Assert the original log is read after an upgrade.

            Args:
                directory (str): Oracle directory object name.
                filename (str): Log filename to read.

            Returns:
                list[str]: Log lines indicating completion and generated dump files.
            """
            assert filename == prefix + ".log"
            return [
                f"/exports/{prefix}_01.dmp",
                f'Job "USER"."{run.oracle_job}" successfully completed',
            ]

        def file_size(self, directory, filename):
            """Return a measured file size.

            Args:
                directory (str): Oracle directory object name.
                filename (str): Dump file name.

            Returns:
                int: Simulated file size in bytes.
            """
            return 123

        def release_master(self, job):
            """Allow cleanup of the retained master table.

            Args:
                job (str): Oracle Data Pump job name.
            """

        def remove_file(self, directory, filename):
            """Record exact legacy files removed by retention.

            Args:
                directory (str): Oracle directory object name.
                filename (str): Filename to delete.
            """
            calls.append(filename)

    @contextmanager
    def connection(*args):
        """Yield a legacy completion transport.

        Args:
            *args (Any): Connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(Remote())

    # Monkeypatch worker connection factory to use test double
    monkeypatch.setattr("oraexport.worker.OracleUtilities.connect", connection)
    worker = Worker(factory)
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
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).state
        == "succeeded"
    )
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
    db.commit()
    # Execute cleanup and verify all expected files are pruned
    worker.cleanup(db)
    assert set(calls) == {prefix + ".log", prefix + "_01.dmp"}
