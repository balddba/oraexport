"""Pydantic validation and Oracle persistence regressions for metadata models."""

from datetime import datetime

import pytest
from dbapi import execute, fetch_records
from pydantic import ValidationError

from oraexport.api.routes.reports import sanitize_csv_cell
from oraexport.models.cell_scalar import CellScalar
from oraexport.models.event_details import EventDetails
from oraexport.models.lease import Lease
from oraexport.models.notification_settings import NotificationSettings
from oraexport.models.row_tuple import RowTuple
from oraexport.models.task import Task
from oraexport.models.user import User
from oraexport.models.user_input import UserInput


@pytest.mark.parametrize(argnames="length", argvalues=[8, 9, 11, 12])
def test_account_password_accepts_eight_or_more_characters(length):
    """Accept account passwords starting at the eight-character boundary."""
    account = UserInput.model_validate(obj={"username": "new-user", "password": "a" * length})
    assert len(account.password.get_secret_value()) == length


@pytest.mark.parametrize(argnames="length", argvalues=[0, 7])
def test_account_password_rejects_fewer_than_eight_characters(length):
    """Reject account passwords below the configured minimum."""
    with pytest.raises(expected_exception=ValidationError, match="at least 8 characters"):
        UserInput.model_validate(obj={"username": "new-user", "password": "a" * length})


def test_model_validates_inputs_and_assignments():
    """Coerce valid inputs and reject invalid assignments and unknown fields."""
    task = Task.model_validate(
        obj={
            "name": "Finance",
            "target_id": "target",
            "schema_name": "FINANCE",
            "directory": "EXPORT_DIR",
            "parallel": "2",
        }
    )
    assert task.parallel == 2
    with pytest.raises(ValidationError):
        task.parallel = "invalid"  # ty: ignore[invalid-assignment] -- Exercise Pydantic assignment validation.
    assert task.parallel == 2
    with pytest.raises(ValidationError):
        User.model_validate(obj={"username": "admin", "password_hash": "hash", "unexpected": "value"})


def test_oracle_mapping_is_excluded_from_pydantic_fields(db):
    """Generate unique model IDs and exclude Oracle mapping metadata from schemas.

    Args:
        db (Connection): Isolated metadata database session.
    """
    user = User(username="metadata-model", password_hash="hash")
    other = User(username="metadata-model-2", password_hash="hash")
    assert len(user.id) == 32
    assert user.id != other.id
    assert user.__tablename__ == "app_users"
    assert user.__pk__ == "id"
    assert not {"__tablename__", "__pk__", "__lobs__"} & user.model_dump().keys()
    assert not {"__tablename__", "__pk__", "__lobs__"} & User.model_json_schema()["properties"].keys()


def test_models_with_named_and_integer_keys_round_trip(db):
    """Persist and update models with string and integer keys without losing types.

    Args:
        db (Connection): Isolated metadata database session.
    """
    timestamp = datetime(2026, 10, 14, 12)
    lease = Lease(name="model-test", owner="worker", expires_at=timestamp)
    settings = NotificationSettings(config='{"smtp_host": "mail.example"}')
    execute(
        db,
        ("INSERT INTO worker_leases (name, owner, expires_at, last_cleanup_at) VALUES (:name, :owner, :expires_at, :last_cleanup_at)"),
        lease.model_dump(mode="python"),
    )
    execute(
        db,
        ("INSERT INTO notification_settings (id, config, secrets) VALUES (:id, :config, :secrets)"),
        settings.model_dump(mode="python"),
        clobs=("config", "secrets"),
    )
    db.commit()
    db.rollback()
    loaded_lease = next(
        iter(
            fetch_records(
                db,
                Lease,
                ("SELECT name, owner, expires_at, last_cleanup_at FROM worker_leases WHERE name = :record_key"),
                {"record_key": "model-test"},
            )
        ),
        None,
    )
    assert loaded_lease is not None
    loaded_settings = next(
        iter(
            fetch_records(
                db,
                NotificationSettings,
                ("SELECT id, config, secrets FROM notification_settings WHERE id = :record_key"),
                {"record_key": 1},
            )
        ),
        None,
    )
    assert loaded_settings is not None
    assert loaded_lease == lease
    assert loaded_lease.expires_at == timestamp
    assert isinstance(loaded_lease.model_dump()["expires_at"], datetime)
    assert loaded_settings == settings
    assert type(loaded_settings.id) is int
    loaded_lease.owner = "new-worker"
    execute(
        db,
        ("UPDATE worker_leases SET owner = :owner WHERE name = :record_key"),
        {"owner": loaded_lease.owner, "record_key": loaded_lease.name},
    )
    loaded_settings.config = "{}"
    execute(
        db,
        ("UPDATE notification_settings SET config = :config WHERE id = :record_key"),
        {"config": loaded_settings.config, "record_key": loaded_settings.id},
        clobs=("config",),
    )
    db.commit()
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Lease,
                    ("SELECT name, owner, expires_at, last_cleanup_at FROM worker_leases WHERE name = :record_key"),
                    {"record_key": "model-test"},
                )
            ),
        ).owner
        == "new-worker"
    )
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    NotificationSettings,
                    ("SELECT id, config, secrets FROM notification_settings WHERE id = :record_key"),
                    {"record_key": 1},
                )
            ),
        ).config
        == "{}"
    )


@pytest.mark.parametrize("value", ["001", "=SUM(A1:A2)", 42, 2.5, True, datetime(year=2026, month=10, day=16), None])
def test_cell_scalar_preserves_types_and_csv_sanitization(value):
    """Validation preserves scalar types and CSV output still escapes formulas."""
    cell = CellScalar.model_validate(obj=value)
    assert type(cell.root) is type(value)
    assert cell.root == value
    sanitized = sanitize_csv_cell(value=cell)
    expected = "'=SUM(A1:A2)" if value == "=SUM(A1:A2)" else value
    assert sanitized.root == expected
    assert type(sanitized.root) is type(value)


def test_event_details_validates_json_without_wrapping_payload():
    """Event serialization preserves nested JSON and rejects arbitrary objects."""
    payload = {"run_id": "test", "errors": ["failure"], "attempts": 1, "reason": None}
    details = EventDetails.model_validate(obj=payload)
    assert details.model_dump(mode="json") == payload
    with pytest.raises(expected_exception=ValidationError):
        EventDetails.model_validate(obj={"unsupported": object()})


def test_row_tuple_preserves_database_values():
    """Rows retain arbitrary driver values without list-to-tuple coercion."""
    marker = object()
    row = RowTuple(root=("001", 42, marker, None))
    assert row.root == ("001", 42, marker, None)
    assert row.root[2] is marker
    with pytest.raises(expected_exception=ValidationError):
        RowTuple.model_validate(obj=["001", 42])
