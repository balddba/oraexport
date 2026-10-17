"""Bounded process-local Oracle metadata connection pool."""

import threading

import oracledb

from oraexport.config import get_settings
from oraexport.oracle_utilities import OracleUtilities

_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()


def metadata_pool() -> oracledb.ConnectionPool:
    """Create the lazy pool once, including when request threads race on startup.

    Returns:
        oracledb.ConnectionPool: Pool with bounded size and acquisition wait.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            settings = get_settings()
            parameters = OracleUtilities.connection_parameters(url=settings.metadata_url.get_secret_value())
            _pool = oracledb.create_pool(
                user=parameters.user,
                password=parameters.password,
                dsn=parameters.dsn,
                tcp_connect_timeout=parameters.tcp_connect_timeout,
                min=0,
                max=settings.metadata_pool_max,
                increment=1,
                getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                wait_timeout=settings.metadata_pool_wait_ms,
                ping_timeout=settings.metadata_call_timeout_ms,
            )
        return _pool


def acquire_metadata_connection() -> oracledb.Connection:
    """Acquire a session and bound every subsequent metadata database round trip.

    Returns:
        oracledb.Connection: Caller-owned session, returned to the pool on close.
    """
    connection = metadata_pool().acquire()
    try:
        connection.call_timeout = get_settings().metadata_call_timeout_ms
    except Exception:
        connection.close()
        raise
    return connection


def close_metadata_pool() -> None:
    """Release process-local sessions during application shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close(force=True)
            _pool = None
