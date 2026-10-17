"""Isolated metadata database and authenticated API fixtures."""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from dbapi import Connection, execute
from fastapi.testclient import TestClient
from oracle_fakes import RemoteConnection

from oraexport import main, oracle_database
from oraexport.api.dependencies import metadata_connection
from oraexport.config import get_settings
from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.main import app
from oraexport.models.credential import Credential
from oraexport.models.defaults import now
from oraexport.models.lease import Lease
from oraexport.models.registration import Registration
from oraexport.models.target import Target
from oraexport.models.task import Task
from oraexport.models.user import User


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    """Use an ephemeral encryption key and test-only configuration.

    Overrides environment variables to isolate test executions with temporary
    encryption secrets, test database URLs, and permissive cookie flags.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture for environment overrides.

    Yields:
        None: Control back to the test context with configured settings.
    """
    # Isolate metadata database URL to prevent tests from connecting to real instances
    monkeypatch.setenv("METADATA_URL", "oracle://test:test@localhost/test")
    # Generate a fresh Fernet encryption key per test session to verify decryption round-trips
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    # Allow insecure cookies for localhost HTTP test client requests
    monkeypatch.setenv("COOKIE_SECURE", "false")
    # Restrict CORS allowed origins to match the Starlette TestClient default host
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://testserver")
    # Clear cached settings so new environment variable values are picked up immediately
    get_settings.cache_clear()
    yield
    # Reset cached settings on teardown to maintain isolation between test functions
    get_settings.cache_clear()


@pytest.fixture
def factory():
    """Create an isolated DB-API substitute; production persistence is exercised.

    Instantiates an in-memory database connection substitute and seeds default leases
    and user roles for admin, operator, and viewer testing.

    Yields:
        Callable[[], Connection]: Factory function returning isolated metadata sessions.
    """
    # Instantiate in-memory Oracle DB-API connection double loaded with schema migrations
    connection = Connection()
    factory = connection.acquire
    # Seed initial system lease and standard testing user accounts
    with factory() as db:
        # Seed an expired worker scheduler lease ready for immediate acquisition
        expired_lease = now() - timedelta(days=1)
        execute(
            db,
            ("INSERT INTO worker_leases (name, owner, expires_at, last_cleanup_at) VALUES (:name, :owner, :expires_at, :last_cleanup_at)"),
            Lease(name="scheduler", owner="unassigned", expires_at=expired_lease).model_dump(mode="python"),
        )
        for role in ("admin", "operator", "viewer"):
            pw_hash = EncryptionSerivce.hash_password("a-strong-test-password")
            user = User(username=role, password_hash=pw_hash, role=role)
            execute(
                db,
                ("INSERT INTO app_users (id, username, password_hash, role, enabled) VALUES (:id, :username, :password_hash, :role, :enabled)"),
                user.model_dump(mode="python"),
            )
        db.commit()
    yield factory
    # Close connection to clean up mock state after test completion
    connection.close()


@pytest.fixture
def db(factory):
    """Yield a metadata session for service-level tests.

    Manages transaction lifecycle by yielding an active session and ensuring rollback
    or closure on fixture exit.

    Args:
        factory (Callable[[], Connection]): Connection factory fixture.

    Yields:
        Connection: Active metadata database session.
    """
    # Yield an active metadata session scoped to the running test case
    with factory() as session:
        yield session


@pytest.fixture
def client(factory, monkeypatch):
    """Provide a test client with request-scoped database sessions.

    Overrides the application database dependency to wire test requests into the
    isolated in-memory metadata database.

    Args:
        factory (Callable): Factory providing test transaction scopes.
        monkeypatch (pytest.MonkeyPatch): Isolate startup migration from Oracle.

    Yields:
        TestClient: FastAPI test client bound to test database sessions.
    """

    def database():
        """Yield the shared test database through the FastAPI dependency.

        Yields:
            Connection: Active metadata session for the API request lifecycle.
        """
        # Create a fresh metadata session per incoming HTTP request
        with factory() as session:
            yield session

    # Override the FastAPI database session dependency with the test factory
    monkeypatch.setattr(target=main, name="migrate_configured_database", value=lambda: None)
    app.dependency_overrides[metadata_connection] = database
    with TestClient(app) as client:
        yield client
    # Restore original dependency bindings to prevent cross-test contamination
    app.dependency_overrides.clear()


@pytest.fixture
def task(db):
    """Seed an encrypted, provisioned target and a single schema policy.

    Creates a full metadata tree consisting of credential, registration, target, and
    an active scheduled task ready for execution testing.

    Args:
        db (Connection): Active database session fixture.

    Returns:
        Task: Seeded and committed Task domain record.
    """
    # Seed encrypted database credentials for Data Pump operations
    encrypted_secret = EncryptionSerivce.encrypt("db-password")
    credential = Credential(username="ORAEXPORT", ciphertext=encrypted_secret)
    execute(
        db,
        ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
        credential.model_dump(mode="python"),
        clobs=("ciphertext",),
    )

    # Seed root database registration referencing the credential
    registration = Registration(name="Test DB", dsn="test:1521/service", credential_id=credential.id)
    execute(
        db,
        ("INSERT INTO registrations (id, name, dsn, credential_id, kind, version) VALUES (:id, :name, :dsn, :credential_id, :kind, :version)"),
        registration.model_dump(mode="python"),
    )

    # Seed provisioned target database referencing the root registration
    target = Target(name="Test PDB", dsn=registration.dsn, directory="EXPORT_DIR", registration_id=registration.id, admin_credential_id=credential.id, export_credential_id=credential.id, provisioned=True)
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

    # Seed backup task configuration for schema-level export
    task = Task(name="Finance", schema_name="FINANCE", directory="EXPORT_DIR", target_id=target.id, next_run_at=now() + timedelta(days=1))
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
    return task


def login(client, role="admin"):
    """Authenticate a test browser with a seeded local account.

    Posts authentication credentials for the specified role to the login endpoint
    and sets session cookies on the test client.

    Args:
        client (TestClient): FastAPI test client.
        role (str): User role to authenticate as (defaults to "admin").

    Returns:
        Response: HTTP response from the authentication endpoint.
    """
    # Send credentials matching the seeded users in the factory fixture
    payload = {"username": role, "password": "a-strong-test-password"}
    return client.post("/api/v1/auth/login", json=payload)


@pytest.fixture(autouse=True)
def recording_oracle_capabilities(monkeypatch):
    """Use operation recorders only for explicit remote connection test doubles.

    Args:
        monkeypatch (pytest.MonkeyPatch): Fixture restoring database methods after each test.
    """

    def recording_database_method(method, remote_name):
        """Preserve database SQL methods except for explicit operation recorders."""

        def call(database, *args):
            """Dispatch to the test recorder or execute the database's SQL implementation."""
            connection = database.oracle_connection
            if isinstance(connection, RemoteConnection):
                return getattr(connection.remote, remote_name)(*args)
            return method(database, *args)

        return call

    for name, remote_name in (
        ("discover", "discover"),
        ("discover_catalog", "catalog"),
        ("start_datapump_job", "start"),
        ("datapump_job_status", "status"),
        ("stop_datapump_job", "cancel"),
        ("verify_datapump_inactive", "verify_inactive"),
        ("release_datapump_master", "release_master"),
        ("verify_directory", "verify_directory"),
        ("read_log", "read_log"),
        ("file_size", "file_size"),
        ("remove_file", "remove_file"),
        ("account_exists", "account_exists"),
        ("provision_export_account", "provision"),
        ("verify_export_task", "verify_task"),
    ):
        monkeypatch.setattr(
            oracle_database.OracleDatabase,
            name,
            recording_database_method(getattr(oracle_database.OracleDatabase, name), remote_name),
        )
