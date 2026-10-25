"""API readiness depends on successful metadata migration."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from oraexport import main


def test_migration_completes_before_api_is_ready(monkeypatch):
    """Run migrations before serving and close the pool on shutdown."""
    migration = Mock()
    close_pool = Mock()
    monkeypatch.setattr(target=main, name="migrate_configured_database", value=migration)
    monkeypatch.setattr(target=main, name="close_metadata_pool", value=close_pool)

    with TestClient(app=main.app) as client:
        migration.assert_called_once_with()
        close_pool.assert_not_called()
        assert client.get(url="/openapi.json").status_code == 200

    close_pool.assert_called_once_with()


def test_failed_migration_aborts_api_startup(monkeypatch):
    """Propagate migration failures and clean up without accepting requests."""
    migration = Mock(side_effect=RuntimeError("Migration failed"))
    close_pool = Mock()
    monkeypatch.setattr(target=main, name="migrate_configured_database", value=migration)
    monkeypatch.setattr(target=main, name="close_metadata_pool", value=close_pool)

    with pytest.raises(expected_exception=RuntimeError, match="Migration failed"), TestClient(app=main.app):
        pytest.fail(reason="API started despite migration failure")

    migration.assert_called_once_with()
    close_pool.assert_called_once_with()
