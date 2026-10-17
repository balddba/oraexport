"""Driver-boundary and metadata transaction regression tests."""

from unittest.mock import MagicMock

import oracledb
import pytest
from dbapi import execute, fetch_records
from pydantic import SecretStr

from oraexport.config import get_settings
from oraexport.models.run import Run
from oraexport.models.user import User
from oraexport.oracle_database import OracleDatabase
from oraexport.oracle_utilities import OracleUtilities


@pytest.mark.parametrize("scheme", ["oracle", "oracle+oracledb"])
def test_connection_parameters(scheme):
    """Both Oracle URL forms preserve encoded credentials and service names.

    Args:
        scheme (str): URL scheme protocol prefix (e.g. 'oracle', 'oracle+oracledb').
    """
    # Parse URL containing URL-encoded characters in username and password
    url = f"{scheme}://meta:p%40ss%2Fword@db:1522/?service_name=META"
    parameters = OracleUtilities.connection_parameters(url=url)
    assert parameters.user == "meta"
    assert parameters.password == "p@ss/word"
    assert "PORT=1522" in parameters.dsn
    assert "SERVICE_NAME=META" in parameters.dsn
    # Verify slash-separated service name format parsing
    slash_params = OracleUtilities.connection_parameters(url="oracle://meta:password@db/META")
    assert "SERVICE_NAME=META" in slash_params.dsn


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "postgresql://user:secret@db/meta",
        "oracle://user:secret@db",
        "oracle://user:secret@db/meta?unexpected=true",
        "oracle://user:secret@db:bad/meta",
    ],
)
def test_reject_non_oracle_or_invalid_urls(url, monkeypatch):
    """Reject invalid configuration without echoing the secret URL.

    Args:
        url (str): Invalid database connection URL under test.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    # Verify parsing exception does not leak credentials in error message
    with pytest.raises(ValueError, match="METADATA_URL must be an Oracle URL") as caught:
        OracleUtilities.connection_parameters(url=url)
    assert "secret" not in str(caught.value)
    # Ensure invalid METADATA_URL environment setting fails fast during configuration loading
    monkeypatch.setenv("METADATA_URL", url)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="METADATA_URL must be an Oracle URL"):
        get_settings()


def test_metadata_dependency_owns_raw_connection(monkeypatch):
    """Request teardown explicitly rolls back and closes its raw driver connection.

    Args:
        monkeypatch (pytest.MonkeyPatch): Driver injection fixture.
    """
    from oraexport.api.dependencies import metadata_connection

    connection = MagicMock()
    open_connection = MagicMock(return_value=connection)
    monkeypatch.setattr("oraexport.api.dependencies.acquire_metadata_connection", open_connection)
    dependency = metadata_connection()
    assert next(dependency) is connection
    open_connection.assert_called_once_with()
    dependency.close()
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


def test_history_state_filters_use_named_binds(db):
    """Run state filters carry values in binds and reject unrecognized states."""
    from oraexport.metadata_service import MetadataService

    repository = MetadataService(db=db)
    assert repository.runs(state="active") == []
    statement, binds = db.statements[-1]
    assert "state = :state" in statement
    assert binds["state"] == "active"
    assert set(binds) == {"state", "task_id", "target_id", "since", "until"}
    with pytest.raises(expected_exception=ValueError, match="Unknown run state"):
        repository.runs(state="x' OR 1=1")
    with pytest.raises(expected_exception=ValueError, match="Unknown run state"):
        repository.run_count(state="x' OR 1=1")
    with pytest.raises(expected_exception=ValueError, match="Unknown run state"):
        list(repository.run_batches(state="x' OR 1=1"))


def test_clob_binding_and_cursor_cleanup():
    """Registration SQL binds large ciphertext as a CLOB and closes every cursor."""
    from oraexport.encryption_serivce import EncryptionSerivce
    from oraexport.metadata_service import MetadataService
    from oraexport.models.registration_input import RegistrationInput

    connection = MagicMock()
    actor = User(username="admin", password_hash="hash")
    body = RegistrationInput(name="CLOB test", dsn="db:1521/service", username="test", password=SecretStr(secret_value="x" * 40000), sysdba=True)
    MetadataService(connection).create_registration(body, actor)
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.setinputsizes.assert_called_once_with(ciphertext=oracledb.DB_TYPE_CLOB)
    sql, binds = cursor.execute.call_args_list[0].args
    assert "INSERT INTO credentials" in sql
    assert EncryptionSerivce.decrypt(binds["ciphertext"]) == "x" * 40000
    assert binds["sysdba"] == 1
    assert connection.cursor.return_value.__exit__.call_count == 3
    connection.commit.assert_called_once()


def test_lob_reads_and_booleans():
    """Direct task queries read LOBs while NUMBER flags become Pydantic booleans."""
    from oraexport.metadata_service import MetadataService
    from oraexport.models.task import Task

    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    lob = MagicMock()
    lob.read.return_value = '["FINANCE"]'
    task = Task(name="Finance", target_id="target", schema_name="FINANCE", directory="EXPORTS")
    values = task.model_dump(mode="python")
    values["schema_names"] = lob
    values["enabled"] = 0
    cursor.description = [(name.upper(),) for name in values]
    cursor.fetchall.return_value = [tuple(values.values())]
    item = MetadataService(connection).tasks()[0]
    assert item.schema_names == '["FINANCE"]'
    assert item.enabled is False
    lob.read.assert_called_once()
    connection.cursor.return_value.__exit__.assert_called_once()


def test_transaction_rollback_and_explicit_updates(db):
    """SQL writes roll back; detached model mutations require an explicit UPDATE.

    Args:
        db (Connection): Metadata database session.
    """
    user = User(username="rollback-user", password_hash="hash")
    execute(
        db,
        ("INSERT INTO app_users (id, username, password_hash, role, enabled) VALUES (:id, :username, :password_hash, :role, :enabled)"),
        user.model_dump(mode="python"),
    )

    # Rollback removes all pending uncommitted entities from the session
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    User,
                    ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE id = :record_key"),
                    {"record_key": user.id},
                )
            ),
            None,
        )
        is None
    )
    execute(
        db,
        ("INSERT INTO app_users (id, username, password_hash, role, enabled) VALUES (:id, :username, :password_hash, :role, :enabled)"),
        user.model_dump(mode="python"),
    )
    db.commit()
    user.enabled = False
    db.commit()
    saved = fetch_records(
        db,
        User,
        "SELECT id, username, password_hash, role, enabled FROM app_users WHERE id = :id",
        {"id": user.id},
    )[0]
    assert saved.enabled is True
    execute(
        db,
        ("UPDATE app_users SET enabled = :enabled WHERE id = :record_key"),
        {"enabled": user.enabled, "record_key": user.id},
    )
    db.commit()
    update_stmt = None
    for stmt in reversed(db.statements):
        if stmt[0].startswith("UPDATE app_users"):
            update_stmt = stmt
            break
    assert update_stmt is not None
    sql = update_stmt[0]
    binds = update_stmt[1]
    assert "SET enabled = :enabled WHERE id = :record_key" in sql
    assert binds == {"enabled": 0, "record_key": user.id}


def test_locking_refreshes_stale_records_without_row_limits(db, task):
    """A lock reads current row state without Oracle's incompatible FETCH clause.

    Args:
        db (Connection): Metadata database session.
        task (Task): Seeded task domain model instance.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    db.commit()
    # Mutate row directly to test refresh on lock
    execute(db, "UPDATE runs SET cancel_requested = 1 WHERE id = :id", {"id": run.id})
    locked = next(
        iter(
            fetch_records(
                db,
                Run,
                ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :id FOR UPDATE SKIP LOCKED"),
                {"id": run.id},
            )
        ),
        None,
    )
    assert locked is not None
    assert locked.id == run.id
    assert locked.cancel_requested is True
    assert run.cancel_requested is False
    # Oracle FOR UPDATE is incompatible with FETCH NEXT row limits
    run_stmt = next(stmt for stmt in reversed(db.statements) if "FROM runs" in stmt[0])
    sql = run_stmt[0]
    assert sql.endswith("FOR UPDATE SKIP LOCKED")
    assert "FETCH" not in sql
