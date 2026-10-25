"""Metadata pool bounds, lifecycle, timeout, and transaction-release regressions."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import oracledb
import pytest

from oraexport import metadata_connections as pools
from oraexport.api.dependencies import metadata_connection


@pytest.fixture
def pool(monkeypatch):
    """Inject a lazy process-local pool without opening an Oracle session.

    Args:
        monkeypatch (pytest.MonkeyPatch): Driver injection fixture.

    Returns:
        MagicMock: Pool creation recorder.
    """
    monkeypatch.setattr(pools, "_pool", None)
    create = MagicMock()
    monkeypatch.setattr(oracledb, "create_pool", create)
    return create


def test_pool_created_once_and_bounded(pool):
    """Concurrent callers share a single pool with finite acquisition and network waits.

    Args:
        pool (MagicMock): Pool creation recorder.
    """
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: pools.metadata_pool(), range(16)))
    pool.assert_called_once()
    assert all(result is pool.return_value for result in results)
    options = pool.call_args.kwargs
    assert options["min"] == 0
    assert options["max"] == 10
    assert options["getmode"] == oracledb.POOL_GETMODE_TIMEDWAIT
    assert options["wait_timeout"] == 5000
    assert options["tcp_connect_timeout"] == 10
    connection = pools.acquire_metadata_connection()
    assert connection.call_timeout == 20000
    pools.close_metadata_pool()
    pool.return_value.close.assert_called_once_with(force=True)
    assert pools._pool is None
    pools.close_metadata_pool()


def test_acquisition_failure_propagates_without_leaking_session(pool):
    """Pool exhaustion propagates promptly and does not create a fallback connection.

    Args:
        pool (MagicMock): Pool creation recorder.
    """
    pool.return_value.acquire.side_effect = oracledb.DatabaseError("pool unavailable")
    with pytest.raises(oracledb.DatabaseError):
        pools.acquire_metadata_connection()
    pool.return_value.close.assert_not_called()


def test_request_failure_returns_session_even_when_rollback_fails(pool):
    """A poisoned request transaction still returns its acquired session.

    Args:
        pool (MagicMock): Pool creation recorder.
    """
    dependency = metadata_connection()
    connection = next(dependency)
    connection.rollback.side_effect = oracledb.DatabaseError("connection lost")
    with pytest.raises(oracledb.DatabaseError):
        dependency.close()
    connection.close.assert_called_once()
