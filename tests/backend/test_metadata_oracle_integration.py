"""Opt-in metadata tests using an isolated real Oracle schema and independent sessions."""

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import oracledb
import pytest

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.application_error import ApplicationError
from oraexport.metadata_service import MetadataService
from oraexport.migrations import REVISION, SCOPE_REVISION, migrate
from oraexport.models.credential import Credential
from oraexport.models.defaults import now
from oraexport.models.registration import Registration
from oraexport.models.target import Target
from oraexport.models.task import Task
from oraexport.models.user import User
from oraexport.models.validators import identifier
from oraexport.oracle_database import OracleDatabase
from oraexport.scheduling import Scheduler
from oraexport.worker import Worker

pytestmark = pytest.mark.oracle


@pytest.fixture
def live_metadata():
    """Create and drop a uniquely named schema using explicit test-only DBA credentials.

    Yields:
        Callable: Factory producing independent, bounded real Oracle connections.
    """
    if os.getenv("ORAEXPORT_LIVE_TESTS") != "1":
        pytest.skip("Set ORAEXPORT_LIVE_TESTS=1 for disposable metadata Oracle tests")
    names = [f"ORAEXPORT_TEST_METADATA_{suffix}" for suffix in ("DSN", "USER", "PASSWORD")]
    values = [os.getenv(name) for name in names]
    if not all(values):
        pytest.skip("Set ORAEXPORT_TEST_METADATA_DSN, USER, and PASSWORD")
    dsn, admin_user, admin_password = values
    tablespace = identifier(os.getenv("ORAEXPORT_TEST_METADATA_TABLESPACE", "USERS"))
    username = "OE_META_" + secrets.token_hex(8).upper()
    password = "T_" + secrets.token_hex(12)
    admin = oracledb.connect(user=admin_user, password=admin_password, dsn=dsn, tcp_connect_timeout=10)
    admin.call_timeout = 20000
    created = False

    def connect():
        """Open a separate real metadata session with bounded network operations."""
        connection = oracledb.connect(user=username, password=password, dsn=dsn, tcp_connect_timeout=10)
        connection.call_timeout = 20000
        return connection

    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE USER {username} IDENTIFIED BY "{password}" DEFAULT TABLESPACE {tablespace} QUOTA 100M ON {tablespace}')
            created = True
            cursor.execute(f"GRANT CREATE SESSION, CREATE TABLE TO {username}")
        with connect() as connection:
            migrate(connection)
        yield connect
    finally:
        try:
            if created:
                with admin.cursor() as cursor:
                    cursor.execute(f"DROP USER {username} CASCADE")
        finally:
            admin.close()


def insert_record(connection, record):
    """Persist one fixture record with real NUMBER flags and CLOB bind types.

    Args:
        connection (oracledb.Connection): Test metadata session.
        record (BaseModel): Fixture record with a known metadata table mapping.
    """
    values = record.model_dump()
    values = {key: int(value) if isinstance(value, bool) else value for key, value in values.items()}
    with connection.cursor() as cursor:
        if record.__lobs__:
            cursor.setinputsizes(**dict.fromkeys(record.__lobs__, oracledb.DB_TYPE_CLOB))
        cursor.execute(
            f"INSERT INTO {record.__tablename__} ({', '.join(values)}) VALUES ({', '.join(':' + key for key in values)})",
            values,
        )


@pytest.fixture
def live_task(live_metadata):
    """Seed a due task with real foreign keys, NUMBER flags, and encrypted credentials.

    Args:
        live_metadata (Callable): Real metadata connection factory.

    Returns:
        tuple[Task, User]: Due task and administrative caller.
    """
    credential = Credential(username="EXPORT_USER", ciphertext=EncryptionSerivce.encrypt("test"))
    registration = Registration(name="Live metadata test", dsn="unused/test", credential_id=credential.id, kind="non_cdb")
    target = Target(
        registration_id=registration.id,
        name="Test target",
        dsn="unused/test",
        admin_credential_id=credential.id,
        export_credential_id=credential.id,
        provisioned=True,
        directory="EXPORTS",
        tablespace="USERS",
    )
    task = Task(
        name="Live task",
        target_id=target.id,
        schema_name="HR",
        directory="EXPORTS",
        next_run_at=now() - timedelta(minutes=1),
    )
    actor = User(username="admin", password_hash="unused", role="admin")
    with live_metadata() as connection:
        for record in (credential, registration, target, task, actor):
            insert_record(connection, record)
        connection.commit()
    return task, actor


def test_live_metadata_migrations_and_rollback(live_metadata):
    """Upgrade an old schema, repeat migration, and verify real rollback isolation.

    Args:
        live_metadata (Callable): Real metadata connection factory.
    """
    with live_metadata() as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE runs DROP COLUMN master_cleanup_pending")
            cursor.execute("UPDATE alembic_version SET version_num = :revision", {"revision": SCOPE_REVISION})
        connection.commit()
        migrate(connection)
        migrate(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == (REVISION,)
        user = User(username="rollback", password_hash="test")
        insert_record(connection, user)
        connection.rollback()
        assert MetadataService(connection).user_by_username("rollback") is None


def test_live_competing_worker_leases(live_metadata):
    """Exactly one simultaneously starting worker acquires the real singleton lease.

    Args:
        live_metadata (Callable): Independent metadata connection factory.
    """
    barrier = threading.Barrier(2, timeout=20)

    def acquire():
        """Contend for leadership using a dedicated Oracle session."""
        with live_metadata() as connection:
            barrier.wait()
            return Worker().acquire_or_renew_lease(connection)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: acquire(), range(2)))
    assert sorted(results) == [False, True]


@pytest.mark.parametrize("scheduled", [False, True])
def test_live_enqueue_race(live_metadata, live_task, scheduled):
    """Task row locks prevent duplicate queue entries from concurrent enqueue paths.

    Args:
        live_metadata (Callable): Independent metadata connection factory.
        live_task (tuple): Due task and actor fixture.
        scheduled (bool): Race a manual run against scheduling or another manual request.
    """
    task, actor = live_task
    barrier = threading.Barrier(2, timeout=20)

    def enqueue(index):
        """Use a separate transaction for each simultaneous enqueue attempt."""
        with live_metadata() as connection:
            barrier.wait()
            if scheduled and index == 0:
                Scheduler().schedule(connection)
                return "scheduled"
            try:
                OracleDatabase(task.target_id, connection).queue_export(task.id, actor)
                return "queued"
            except ApplicationError as exc:
                connection.rollback()
                if exc.status_code != 409:
                    raise
                return "busy"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue, range(2)))
    with live_metadata() as connection:
        assert MetadataService(connection).run_count() == 1
        saved = MetadataService(connection).tasks()[0]
        if scheduled:
            assert saved.next_run_at is not None
            assert saved.next_run_at > now() - timedelta(seconds=5)
        # Exercise real CLOB reads and bounded report queries too.
        batch = next(MetadataService(db=connection).run_batches(batch_size=1))
        assert len(batch) == 1
        assert MetadataService(connection).dump_sizes([batch[0].id]) == {batch[0].id: None}
    if not scheduled:
        assert sorted(results) == ["busy", "queued"]
