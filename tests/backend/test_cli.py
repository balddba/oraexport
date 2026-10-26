"""Administrator password recovery without existing application credentials."""

import sys
from copy import deepcopy

import pytest
from dbapi import Cursor

from oraexport import cli
from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.application_error import ApplicationError
from oraexport.metadata_service import MetadataService


@pytest.fixture
def reset_admin(monkeypatch, db):
    """Run the real CLI against isolated metadata with private prompts replaced."""
    monkeypatch.setattr(target=cli.oracledb, name="connect", value=lambda **kwargs: db)

    def invoke(username="admin", password="new-strong-admin-password", confirmation=None):
        """Supply a username and two password prompt responses."""
        answers = iter([password, password if confirmation is None else confirmation])
        monkeypatch.setattr(target=sys, name="argv", value=["oraexport", "reset-admin-password", "--username", username])
        monkeypatch.setattr(target=cli.getpass, name="getpass", value=lambda prompt: next(answers))
        cli.main()

    return invoke


def test_reset_changes_password_and_revokes_only_target_sessions(reset_admin, db, capsys):
    """Reject the old password and preserve other accounts and their sessions."""
    service = MetadataService(db=db)
    admin, _ = service.login(username="admin", password="a-strong-test-password")
    operator, _ = service.login(username="operator", password="a-strong-test-password")
    operator_before = service.user_by_username(username="operator")

    reset_admin(password="eight123")

    output = capsys.readouterr().out
    assert "password reset" in output
    assert "eight123" not in output
    assert {session["user_id"] for session in db.tables["login_sessions"]} == {operator.id}
    audit = db.tables["audit_events"][-1]
    assert audit["action"] == "admin_password_reset_cli"
    assert audit["resource_id"] == admin.id
    assert audit["user_id"] is None
    assert service.user_by_username(username="operator") == operator_before
    with pytest.raises(expected_exception=ApplicationError):
        service.login(username="admin", password="a-strong-test-password")
    updated, _ = service.login(username="admin", password="eight123")
    assert updated.id == admin.id
    assert updated.role == "admin"
    assert updated.enabled


@pytest.mark.parametrize(argnames="username", argvalues=["missing", "operator", "viewer", "disabled"])
def test_reset_rejects_ineligible_accounts(reset_admin, db, username):
    """Never create, promote, or enable accounts during password recovery."""
    if username == "disabled":
        with db.cursor() as cursor:
            cursor.execute(statement="UPDATE app_users SET enabled = :enabled WHERE username = :username", parameters={"enabled": 0, "username": "admin"})
        db.commit()
        username = "admin"
    before = deepcopy(x=db.tables)
    with pytest.raises(expected_exception=SystemExit) as error:
        reset_admin(username=username)
    assert error.value.code == 2
    assert db.tables == before


@pytest.mark.parametrize(argnames=("password", "confirmation"), argvalues=[("seven12", "seven12"), ("eight123", "different")])
def test_reset_rejects_invalid_passwords(reset_admin, db, password, confirmation):
    """Leave persisted credentials untouched for short or mismatched passwords."""
    before = deepcopy(x=db.tables)
    with pytest.raises(expected_exception=SystemExit):
        reset_admin(password=password, confirmation=confirmation)
    assert db.tables == before


def test_reset_rolls_back_password_and_sessions_on_audit_failure(reset_admin, db, monkeypatch):
    """Recover atomically when a later database write fails."""
    MetadataService(db=db).login(username="admin", password="a-strong-test-password")
    before = deepcopy(x=db.tables)
    original_execute = Cursor.execute

    def execute(self, statement, parameters=None):
        """Fail only the audit insert after the credential and session changes."""
        if statement.startswith("INSERT INTO audit_events"):
            raise RuntimeError("Audit unavailable")
        return original_execute(self=self, statement=statement, parameters=parameters)

    monkeypatch.setattr(target=Cursor, name="execute", value=execute)
    with pytest.raises(expected_exception=RuntimeError, match="Audit unavailable"):
        reset_admin()
    assert db.tables == before
    admin = MetadataService(db=db).user_by_username(username="admin")
    assert EncryptionSerivce.verify_password(value="a-strong-test-password", digest=admin.password_hash)
