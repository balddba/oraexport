"""Validation, schedule, file ownership, and retention regression tests."""

import json
from datetime import datetime, timedelta

import pytest
from dbapi import execute, fetch_records
from pydantic import ValidationError

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.models.backup_file import BackupFile
from oraexport.models.defaults import now, uid
from oraexport.models.export_run import ExportRun
from oraexport.models.job_status import JobStatus
from oraexport.models.provision_input import ProvisionInput
from oraexport.models.run import Run
from oraexport.models.schedule_input import ScheduleInput
from oraexport.models.target import Target
from oraexport.models.task_input import TaskInput
from oraexport.models.user import User
from oraexport.oracle_database import OracleDatabase


def test_encryption_and_passwords():
    """Encrypted secrets and password digests never contain their plaintext."""
    # Verify reversible encryption prevents plaintext leakage in ciphertext
    ciphertext = EncryptionSerivce.encrypt("secret-password")
    assert "secret-password" not in ciphertext
    assert EncryptionSerivce.decrypt(ciphertext) == "secret-password"
    # Verify salted one-way password hashing and verification
    digest = EncryptionSerivce.hash_password("local-password")
    assert EncryptionSerivce.verify_password("local-password", digest)
    assert not EncryptionSerivce.verify_password("wrong", digest)


@pytest.mark.parametrize(
    "patch",
    [
        {"parallel": 0},
        {"cron": "* * * * * *"},
        {"timezone": "MadeUp/Zone"},
        {"schema_name": "SYS"},
        {"directory": "DIR; DROP USER SYS"},
        {"unexpected": 1},
    ],
)
def test_invalid_task_fields(patch: dict[str, str | int]) -> None:
    """Reject invalid limits, identifiers, cron syntax, and unknown fields.

    Args:
        patch (dict[str, str | int]): Invalid task field values.
    """
    # Merge patch with valid base inputs and verify schema validation rejects it
    values = {"name": "Test", "target_id": "id", "schema_name": "HR", "directory": "DUMP_DIR"}
    with pytest.raises(ValidationError):
        TaskInput.model_validate(values | patch)


def test_preview_and_normalization(db, task):
    """Preview is secret-free and contains only validated SQL identifiers."""
    # Ensure generated DDL preview masks secrets and includes flashback privileges
    options = ProvisionInput(directory="exports", consistent_snapshot=True)
    selected_record = next(
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
    assert selected_record is not None
    selected_record.export_credential_id = None
    execute(
        db,
        ("UPDATE targets SET export_credential_id = :export_credential_id WHERE id = :record_key"),
        {
            "export_credential_id": selected_record.export_credential_id,
            "record_key": selected_record.id,
        },
    )
    user = next(
        iter(
            fetch_records(
                db,
                User,
                ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE username = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": "admin"}, row_limit=1),
            )
        ),
        None,
    )
    assert user is not None
    preview = OracleDatabase(task.target_id, db).provision_preview(options, user)
    sql = "\n".join(preview.sql)
    assert "DIRECTORY EXPORTS" in sql
    assert "FLASHBACK ANY TABLE" in sql
    assert "<generated password>" in sql
    assert ScheduleInput(cron="0 2 * * *", timezone="UTC")


def test_dst_gap_and_repeat():
    """Skip nonexistent wall times and never run the repeated local time twice."""
    # Spring forward gap: 2:30 AM local does not exist; schedule advances to next day
    gap_target = datetime(2026, 3, 9, 6, 30)
    assert OracleDatabase.next_occurrence("30 2 * * *", "America/Detroit", datetime(2026, 3, 8, 0)) == gap_target
    # Fall back repeat: 1:30 AM occurs twice; advance to first occurrence then jump full cycle
    first = OracleDatabase.next_occurrence("30 1 * * *", "America/Detroit", datetime(2026, 11, 1, 0))
    assert first == datetime(2026, 11, 1, 5, 30)
    assert OracleDatabase.next_occurrence("30 1 * * *", "America/Detroit", first) == datetime(2026, 11, 2, 6, 30)
    # Standard UTC cron step
    assert OracleDatabase.next_occurrence("0 2 * * *", "UTC", datetime(2026, 10, 9, 2)) == datetime(2026, 10, 10, 2)


def test_queue_snapshot_and_duplicates(db, task):
    """Queue settings are immutable and concurrent task work is rejected.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model instance.
    """
    # Enqueue a new run and commit transaction
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Mutate task settings; queued snapshot must retain original values
    task.parallel = 7
    execute(
        db,
        ("UPDATE tasks SET parallel = :parallel WHERE id = :record_key"),
        {"parallel": task.parallel, "record_key": task.id},
    )
    db.commit()
    assert json.loads(run.snapshot)["parallel"] == 1
    assert len(run.oracle_job) == 30
    # Duplicate active queue attempt must fail fast
    with pytest.raises(ValueError, match="already"):
        OracleDatabase(task.target_id, db).enqueue(task)


@pytest.mark.parametrize(
    ("state", "errors", "cancel", "expected"),
    [
        ("EXECUTING", [], False, "running"),
        ("COMPLETED", [], True, "succeeded"),
        ("COMPLETED", ["ORA-12345"], False, "completed_with_errors"),
        ("STOPPED", [], True, "cancelled"),
        ("STOPPED", [], False, "failed"),
    ],
)
def test_terminal_classification(state, errors, cancel, expected):
    """Use Oracle job state and errors rather than percentage to determine outcomes.

    Args:
        state (str): Oracle Data Pump status state string.
        errors (list[str]): List of Oracle error messages.
        cancel (bool): Boolean flag indicating whether user requested cancellation.
        expected (str): Expected classified terminal state name.
    """
    # Map raw DBMS_DATAPUMP status to application domain lifecycle states
    run = ExportRun(state="running", cancel_requested=cancel)
    assert run.classify(status=JobStatus(state=state, progress=100, errors=errors)) == expected


def test_owned_files_only(db, task):
    """Ignore foreign filenames, templates, and log path components in file manifests.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model instance.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Simulate Data Pump emitting a mix of owned, template, and foreign files
    prefix = OracleDatabase.file_prefix(run.id, json.loads(run.snapshot))
    dmp_file = f"/exports/{prefix}_01.dmp"
    template_file = f"{prefix}_%L.dmp"
    OracleDatabase(run.target_id, db).update_status(run, JobStatus(state="EXECUTING", progress=50, files=[dmp_file, "unrelated.dmp", template_file]))
    db.commit()
    # Verify only files within the exact owned namespace were recorded
    files = fetch_records(
        db,
        BackupFile,
        ("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :run_id"),
        {"run_id": run.id},
    )
    expected_files = {f"{prefix}.log", f"{prefix}_01.dmp"}
    assert {f.filename for f in files} == expected_files
    # Verify path parsing across Windows backslashes and relative paths
    win_path = f"C:\\exports\\{prefix}_01.dmp"
    assert OracleDatabase.owned_filename(run.id, win_path, json.loads(run.snapshot))
    assert not OracleDatabase.owned_filename(run.id, "../foreign.dmp")


def test_retention_preserves_latest_success_and_active(db, task):
    """Expire old terminal runs without deleting the latest success or active work.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model instance.
    """
    runs = []
    # Decompose enumeration loop unpacking into indexed single lines
    states = ["succeeded", "succeeded", "failed", "running"]
    for item in enumerate(states):
        index = item[0]
        state = item[1]
        run_id = uid()
        run = Run(id=run_id, task_id=task.id, target_id=task.target_id, snapshot="{}", occurrence=str(index), oracle_job="OE_" + run_id[:27], state=state, finished_at=now() - timedelta(days=100 - index))
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
            run.model_dump(mode="python"),
            clobs=("snapshot", "errors", "messages"),
        )
        runs.append(run)
    db.commit()
    # Verify retention policy marks only expired terminal non-latest runs
    assert {r.id for r in OracleDatabase(task.target_id, db).expired_runs(task)} == {
        runs[0].id,
        runs[2].id,
    }


def test_log_recovery_requires_named_completion():
    """A missing job or unrelated completion text never produces false success."""
    run_id = uid()
    # Mismatched job name in log line must be ignored
    unrelated_log = ['Job "USER"."OTHER" successfully completed']
    assert OracleDatabase.recover_from_log("OE_JOB", run_id, unrelated_log) is None
    # Matching job completion banner extracts state and dump files
    log_lines = [f"/exports/oe_{run_id}_01.dmp"]
    log_lines.append('Job "ORAEXPORT"."OE_JOB" successfully completed at Fri Oct 9')
    recovered = OracleDatabase.recover_from_log("OE_JOB", run_id, log_lines)
    assert recovered is not None
    assert recovered.state == "COMPLETED"
    assert recovered.files == [f"oe_{run_id}_01.dmp"]
    # Error banner in log file maps to completed_with_errors classification
    err_lines = ["ORA-39166: Object not found"]
    err_lines.append('Job "ORAEXPORT"."OE_JOB" completed with 1 error(s)')
    recovered = OracleDatabase.recover_from_log("OE_JOB", run_id, err_lines)
    assert recovered is not None
    assert ExportRun(state="running").classify(status=recovered) == "completed_with_errors"


def test_final_recovery_preserves_previously_recorded_errors(db, task):
    """A later completion observation cannot erase earlier Data Pump errors.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model instance.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Record non-terminal execution with logged error
    OracleDatabase(run.target_id, db).update_status(run, JobStatus(state="EXECUTING", errors=["ORA-39166: object missing"]))
    # Applying subsequent 100% completion must not erase prior error status
    OracleDatabase(run.target_id, db).update_status(run, JobStatus(state="COMPLETED", progress=100))
    assert run.state == "completed_with_errors"


def test_export_scope_normalization_and_legacy_compatibility():
    """Normalize a selected set and accept existing single-schema task inputs."""
    # Legacy schema_name field promotes to schema_names list
    legacy = TaskInput(name="Scope", target_id="target", directory="EXPORTS", schema_name="hr")
    assert legacy.schema_names == ["HR"]
    assert legacy.export_mode == "SCHEMA"
    # Deduplicate and uppercase schema names
    multiple = TaskInput(name="Scope", target_id="target", directory="EXPORTS", schema_names=["hr", "finance", "HR"])
    assert multiple.schema_names == ["HR", "FINANCE"]
    assert multiple.schema_name == "HR"
    # Full database export mode requires empty schema selection
    full = TaskInput(name="Scope", target_id="target", directory="EXPORTS", export_mode="FULL")
    assert full.schema_names == []
    assert full.schema_name == ""


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"schema_names": []},
        {"schema_names": ["SYS"]},
        {"schema_names": ["HR', 'SYS"]},
        {"schema_names": ["HR"] * 201},
        {"schema_names": ["HR"], "schema_name": "FINANCE"},
        {"export_mode": "FULL", "schema_names": ["HR"]},
        {"export_mode": "FULL", "schema_name": "HR"},
        {"export_mode": "OTHER"},
    ],
)
def test_reject_invalid_export_scope(scope: dict[str, str | list[str]]) -> None:
    """Reject empty, ambiguous, oversized, and unsafe schema selections.

    Args:
        scope (dict[str, str | list[str]]): Invalid export scope payload.
    """
    # Enforce Pydantic validation on schema lists and full export combinations
    with pytest.raises(ValidationError):
        TaskInput.model_validate({"name": "Scope", "target_id": "target", "directory": "EXPORTS"} | scope)
