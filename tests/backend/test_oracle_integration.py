"""Opt-in live Oracle tests: use disposable databases and dedicated DIRECTORY paths only."""

import contextlib
import json
import os
import secrets
import time
from datetime import timedelta

import pytest
from dbapi import execute, fetch_records

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.models.backup_file import BackupFile
from oraexport.models.credential import Credential
from oraexport.models.defaults import now
from oraexport.models.provision_input import ProvisionInput
from oraexport.models.registration import Registration
from oraexport.models.run import Run
from oraexport.models.target import Target
from oraexport.models.task import Task
from oraexport.models.task_input import TaskInput
from oraexport.oracle_database import OracleDatabase
from oraexport.worker import Worker

pytestmark = pytest.mark.oracle


def admin_for(prefix: str):
    """Load an explicit integration-test connection, never production app credentials.

    Args:
        prefix (str): Environment variable prefix key (e.g., 'PDB', 'NONCDB', 'CDB').

    Returns:
        tuple[str, Credential]: DSN connection string and admin Credential record.
    """
    # Check live testing opt-in flag; skip if unset
    dsn = os.getenv(f"ORAEXPORT_TEST_{prefix}_DSN")
    if os.getenv("ORAEXPORT_LIVE_TESTS") != "1" or not dsn:
        pytest.skip(f"Set ORAEXPORT_LIVE_TESTS=1 and ORAEXPORT_TEST_{prefix}_DSN")
    username = os.getenv(f"ORAEXPORT_TEST_{prefix}_USER", "system")
    password = os.getenv(f"ORAEXPORT_TEST_{prefix}_PASSWORD")
    if not password:
        pytest.skip(f"Missing ORAEXPORT_TEST_{prefix}_PASSWORD")
    is_sysdba = os.getenv(f"ORAEXPORT_TEST_{prefix}_SYSDBA") == "1"
    credential = Credential(username=username, ciphertext=EncryptionSerivce.encrypt(password), sysdba=is_sysdba)
    return dsn, credential


@pytest.mark.parametrize(("prefix", "kind"), [("PDB", "pdb"), ("NONCDB", "non_cdb"), ("CDB", "cdb")])
def test_live_topology(prefix, kind, db):
    """Validate CDB discovery and directly connected PDB/non-CDB detection.

    Args:
        prefix (str): Target environment variable prefix ('PDB', 'NONCDB', 'CDB').
        kind (str): Expected discovered architecture kind ('pdb', 'non_cdb', 'cdb').
        db (Connection): Metadata database session.
    """
    # Decompose admin connection tuple unpacking into single lines
    admin_info = admin_for(prefix)
    dsn = admin_info[0]
    admin = admin_info[1]
    with OracleDatabase("integration-test", db).connected(dsn, admin) as oracle:
        result = oracle.discover()
        assert result.kind == kind
        if kind == "cdb":
            assert all(pdb.name != "PDB$SEED" for pdb in result.pdbs)
            with pytest.raises(ValueError, match="directly"):
                oracle.discover_catalog()
        else:
            assert isinstance(oracle.discover_catalog().directories, list)


@pytest.fixture(params=["PDB", "NONCDB"])
def live_target(request, db):
    """Provision unique export/schema accounts and clean only their owned artifacts.

    Args:
        request (pytest.FixtureRequest): Pytest request object containing parametrization.
        db (Connection): Metadata database session.

    Yields:
        tuple[Task, str, Credential]: Seeded task, live DSN, and export credential.
    """
    prefix = request.param
    admin_info = admin_for(prefix)
    dsn = admin_info[0]
    admin = admin_info[1]
    directory = os.getenv(f"ORAEXPORT_TEST_{prefix}_DIRECTORY")
    if not directory:
        pytest.skip(f"Missing ORAEXPORT_TEST_{prefix}_DIRECTORY")
    # Decompose username definitions into single lines
    suffix = secrets.token_hex(5).upper()
    export_user = f"OEIT_{suffix}"
    schema = f"OEIS_{suffix}"
    password = "Oe_" + secrets.token_hex(24)
    # Decompose provisioning inputs into single-line definitions
    tb_space = os.getenv(f"ORAEXPORT_TEST_{prefix}_TABLESPACE", "USERS")
    options = ProvisionInput(export_username=export_user, directory=directory, tablespace=tb_space, consistent_snapshot=True)
    credential = Credential(username=export_user, ciphertext=EncryptionSerivce.encrypt(password))
    with OracleDatabase("integration-test", db).connected(dsn, admin) as oracle:
        oracle.provision_export_account(options, password)
        oracle.provision_export_account(options, None)  # Idempotent repeat of owned account grants.
        oracle.oracle_cursor.execute(f'CREATE USER {schema} IDENTIFIED BY "{password}" DEFAULT TABLESPACE {options.tablespace} QUOTA 100M ON {options.tablespace}')
        oracle.oracle_cursor.execute(f"CREATE TABLE {schema}.fixture AS SELECT LEVEL id FROM dual CONNECT BY LEVEL <= 10000")
        oracle.oracle_connection.commit()
    execute(
        db,
        ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
        admin.model_dump(mode="python"),
        clobs=("ciphertext",),
    )
    execute(
        db,
        ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
        credential.model_dump(mode="python"),
        clobs=("ciphertext",),
    )

    registration = Registration(name=export_user, dsn=dsn, credential_id=admin.id)
    execute(
        db,
        ("INSERT INTO registrations (id, name, dsn, credential_id, kind, version) VALUES (:id, :name, :dsn, :credential_id, :kind, :version)"),
        registration.model_dump(mode="python"),
    )

    target = Target(name=export_user, registration_id=registration.id, dsn=dsn, admin_credential_id=admin.id, export_credential_id=credential.id, directory=directory, provisioned=True)
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

    task = Task(name=export_user, target_id=target.id, schema_name=schema, directory=directory, consistent_snapshot=True, next_run_at=now() + timedelta(days=1))
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
        task.model_dump(mode="python"),
        clobs=("schema_names",),
    )
    db.commit()
    try:
        with OracleDatabase("integration-test", db).connected(dsn, credential) as oracle:
            oracle.verify_export_task(TaskInput(name=task.name, target_id=target.id, schema_name=schema, directory=directory, consistent_snapshot=True))
        yield task, dsn, credential
    finally:
        # Cleanup by exact test-owned manifest, never by directory enumeration.
        with OracleDatabase("integration-test", db).connected(dsn, credential) as oracle:
            for run in fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE task_id = :task_id"),
                {"task_id": task.id},
            ):
                with contextlib.suppress(Exception):
                    oracle.stop_datapump_job(run.oracle_job)

            for file in fetch_records(
                db,
                BackupFile,
                ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id IN (SELECT id FROM runs WHERE task_id = :task_id)"),
                {"task_id": task.id},
            ):
                with contextlib.suppress(Exception):
                    oracle.remove_file(file.directory, file.filename)

        with OracleDatabase("integration-test", db).connected(dsn, admin) as oracle:
            oracle.oracle_cursor.execute(f"DROP USER {export_user} CASCADE")
            oracle.oracle_cursor.execute(f"DROP USER {schema} CASCADE")


def test_live_export_recovery_and_retention(live_target, factory, db):
    """Run an export, monitor with a new worker, measure files, and exercise retention.

    Args:
        live_target (tuple[Task, str, Credential]): Seeded target task, DSN, and credentials.
        factory (Callable[[], Connection]): Connection factory fixture.
        db (Connection): Metadata database session.
    """
    task = live_target[0]
    dsn = live_target[1]
    credential = live_target[2]
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    first = Worker(factory)
    assert first.claim_run(db) == run.id
    first.database(
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
    recovery = Worker(factory)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if recovery.database(
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
        ).monitor_export(run.id):
            break
        time.sleep(2)
    assert run.state == "succeeded", run.errors + run.messages
    files = fetch_records(
        db,
        BackupFile,
        ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :run_id"),
        {"run_id": run.id},
    )
    assert any(file.kind == "dump" and file.size_bytes is not None and file.size_bytes > 0 for file in files)
    run.finished_at = now() - timedelta(days=100)
    execute(
        db,
        ("UPDATE runs SET finished_at = :finished_at WHERE id = :record_key"),
        {"finished_at": run.finished_at, "record_key": run.id},
    )
    db.commit()
    recovery.cleanup(db)
    assert all(file.deleted_at is None for file in files)  # Most recent success retained.
    run.state = "failed"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )  # Test-only transition to make this disposable artifact eligible.
    db.commit()
    recovery.cleanup(db)
    assert all(file.deleted_at for file in files)
    with OracleDatabase("integration-test", db).connected(dsn, credential) as oracle:
        assert all(oracle.file_size(file.directory, file.filename) is None for file in files)


def test_live_cancel_defined_job(live_target, db):
    """Exercise Oracle stop/reattach on a defined job before data export can finish.

    Args:
        live_target (tuple[Task, str, Credential]): Seeded target task, DSN, and credentials.
        db (Connection): Metadata database session.
    """
    task = live_target[0]
    dsn = live_target[1]
    credential = live_target[2]
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    with OracleDatabase("integration-test", db).connected(dsn, credential) as oracle:
        open_binds = {"operation": "EXPORT", "job_mode": "SCHEMA", "remote_link": None, "job_name": run.oracle_job}
        handle = oracle.oracle_cursor.callfunc(name="DBMS_DATAPUMP.OPEN", return_type=int, keyword_parameters=open_binds)
        log_name = f"{OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))}.log"
        file_binds = {"handle": handle, "filename": log_name, "directory": task.directory, "filesize": None, "filetype": 3}
        oracle.oracle_cursor.callproc(name="DBMS_DATAPUMP.ADD_FILE", keyword_parameters=file_binds)
        oracle.oracle_cursor.callproc(name="DBMS_DATAPUMP.DETACH", keyword_parameters={"handle": handle})
        oracle.stop_datapump_job(run.oracle_job)
        status = oracle.datapump_job_status(run.oracle_job)
        assert status.state in {"STOPPED", "NOT RUNNING"}
