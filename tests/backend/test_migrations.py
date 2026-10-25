"""Direct Oracle migration SQL, restart, and existing-schema compatibility checks."""

import re
from typing import cast

import oracledb
import pytest

from oraexport.cli import main as cli_main
from oraexport.migrations import (
    LEGACY_REVISION,
    REVISION,
    migrate,
    preview_sql,
    schema_statements,
)


class MigrationConnection:
    """Model Oracle catalog queries and independently committed DDL.

    Acts as an in-memory double simulating Oracle data dictionary views
    (user_tables, user_indexes, user_tab_columns) and implicit DDL auto-commits.
    """

    def __init__(self):
        """Initialize an empty Oracle catalog substitute."""
        self.tables = {}
        self.indexes = set()
        self.revisions = []
        self.seeded = False
        self.result = []
        self.executed = []
        self.commits = 0
        self.fail_at = None

    def cursor(self):
        """Use the catalog as a small cursor substitute.

        Returns:
            MigrationConnection: Self reference serving as active cursor double.
        """
        return self

    def __enter__(self):
        """Enter a catalog cursor scope.

        Returns:
            MigrationConnection: Active cursor double context.
        """
        return self

    def __exit__(self, *exc):
        """Exit the catalog cursor scope.

        Args:
            *exc (Any): Exception details tuple.

        Returns:
            None: Does not suppress exceptions.
        """

    def execute(self, sql, binds=None):
        """Model the runner's Oracle dictionary queries, DDL, and bound DML.

        Args:
            sql (str): SQL statement or DDL command to execute.
            binds (dict[str, Any] | None): Optional dictionary of bind parameters.

        Raises:
            ConnectionError: If simulated DDL failure trigger matches the SQL.
            AssertionError: If an unrecognized statement is encountered.
        """
        self.executed.append((sql, binds))
        self.result = []
        # Inject simulated failure during DDL statement execution
        if self.fail_at and self.fail_at in sql:
            raise ConnectionError("Simulated DDL interruption")
        if sql == "SELECT table_name FROM user_tables":
            self.result = [(name.upper(),) for name in self.tables]
        elif sql == "SELECT index_name FROM user_indexes":
            self.result = [(name.upper(),) for name in self.indexes]
        elif sql == "SELECT version_num FROM alembic_version":
            self.result = [(revision,) for revision in self.revisions]
        elif sql.startswith("SELECT column_name FROM user_tab_columns"):
            assert binds is not None
            table_name = binds["table_name"].lower()
            self.result = [(name,) for name in self.tables[table_name]]
        elif sql.startswith("CREATE TABLE alembic_version"):
            self.tables["alembic_version"] = ["VERSION_NUM"]
        elif sql.startswith("CREATE TABLE"):
            match = re.match(pattern=r"CREATE TABLE (\w+)", string=sql)
            assert match is not None
            name = match[1]
            assert name not in self.tables
            # Parse column definitions excluding constraint lines into single lines
            cols = []
            for line in sql.splitlines()[1:-1]:
                stripped = line.strip()
                if not stripped.startswith(("PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT")):
                    cols.append(stripped.split()[0].upper())
            self.tables[name] = cols
        elif sql.startswith("ALTER TABLE"):
            col_match = re.match(r"ALTER TABLE (\w+) ADD \((\w+)", sql)
            assert col_match is not None
            self.tables[col_match[1]].append(col_match[2].upper())
        elif sql.startswith("UPDATE alembic_version"):
            assert binds is not None
            self.revisions = [binds["revision"]]
        elif sql.startswith("CREATE INDEX"):
            match = re.match(pattern=r"CREATE INDEX (\w+)", string=sql)
            assert match is not None
            name = match[1]
            assert name not in self.indexes
            self.indexes.add(name)
        elif sql.startswith("INSERT INTO worker_leases"):
            self.seeded = True
        elif sql.startswith("INSERT INTO alembic_version"):
            assert binds is not None
            self.revisions.append(binds["revision"])
        else:
            raise AssertionError(sql)

    def fetchall(self):
        """Return simulated Oracle dictionary results.

        Returns:
            list[tuple[Any, ...]]: Buffered query rows.
        """
        return self.result

    def commit(self):
        """Record the migration's explicit final commit."""
        self.commits += 1


def test_oracle_preview_sql():
    """Schema previews include real Oracle types, relationships, and scheduling indexes."""
    # Validate generated DDL syntax, datatypes, and constraints
    sql = preview_sql()
    assert "CREATE TABLE runs" in sql
    assert "CREATE TABLE backup_files" in sql
    assert "CLOB" in sql
    assert "VARCHAR2" in sql
    assert "NUMBER(1)" in sql
    assert "FOREIGN KEY (task_id) REFERENCES tasks (id)" in sql
    assert "CONSTRAINT uq_run_occurrence UNIQUE (task_id, occurrence)" in sql
    assert "CONSTRAINT uq_run_file UNIQUE (run_id, directory, filename)" in sql
    assert "CREATE INDEX ix_tasks_next_run_at" in sql
    assert "INSERT INTO worker_leases" in sql
    assert len(schema_statements()) == 17


def test_migrate_new_and_existing_schema():
    """Install once, then adopt the existing revision without recreating any tables."""
    # First migration run creates all tables and stamps initial revision
    connection = MigrationConnection()
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert connection.seeded
    assert connection.revisions == [REVISION]
    assert len(connection.tables) == 13
    connection.executed.clear()
    # Second migration run against already-provisioned schema must be idempotent
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert not any(item[0].startswith("CREATE") for item in connection.executed)
    assert connection.revisions == [REVISION]
    assert connection.commits == 2


def test_migrate_resumes_after_ddl_interruption():
    """Oracle's committed partial DDL is reused on a retry."""
    connection = MigrationConnection()
    connection.fail_at = "CREATE TABLE targets"
    # Interrupted migration raises error after partial DDL commits
    with pytest.raises(ConnectionError):
        migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert "credentials" in connection.tables
    assert not connection.revisions
    # Retrying resumes from existing committed tables without recreation errors
    connection.fail_at = None
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert connection.revisions == [REVISION]
    assert connection.seeded


def test_migrate_rejects_unknown_revision():
    """Do not apply DDL against a schema with an unsupported revision."""
    connection = MigrationConnection()
    connection.tables["alembic_version"] = ["VERSION_NUM"]
    connection.revisions = ["unexpected"]
    # Unrecognized revisions trigger safe abort
    with pytest.raises(RuntimeError, match="Unknown"):
        migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert not any(item[0].startswith("CREATE") for item in connection.executed)


def test_migrate_rejects_mismatched_existing_table():
    """Never stamp a partially incompatible existing schema as current."""
    connection = MigrationConnection()
    # Incompatible table structure triggers validation error
    connection.tables["app_users"] = ["WRONG_COLUMN"]
    with pytest.raises(RuntimeError, match="app_users"):
        migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert not connection.revisions


def test_cli_migration_preview_does_not_connect(monkeypatch, capsys):
    """The CLI prints reviewable SQL without loading secrets or reaching Oracle.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
        capsys (pytest.CaptureFixture): Pytest standard output capture fixture.
    """
    # Verify CLI migrate --dry-run prints DDL script without opening DB connections
    monkeypatch.setattr("sys.argv", ["oraexport", "migrate", "--dry-run"])
    cli_main()
    assert "CREATE TABLE runs" in capsys.readouterr().out


def test_upgrade_preserves_legacy_tasks_and_resumes_ddl():
    """Resume each independently committed task-column addition and stamp only after success."""
    connection = MigrationConnection()
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    # Simulate legacy schema state with missing columns
    connection.tables["tasks"] = connection.tables["tasks"][:-2]
    connection.revisions = [LEGACY_REVISION]
    connection.executed.clear()
    connection.fail_at = "ALTER TABLE tasks ADD (schema_names"
    with pytest.raises(ConnectionError):
        migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert connection.revisions == [LEGACY_REVISION]
    assert connection.tables["tasks"][-1] == "EXPORT_MODE"
    # Resume upgrade and ensure both columns are added and revision stamped
    connection.fail_at = None
    connection.executed.clear()
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert connection.revisions == [REVISION]
    assert connection.tables["tasks"][-2:] == ["EXPORT_MODE", "SCHEMA_NAMES"]
    assert not any(item[0].startswith("CREATE") for item in connection.executed)
    assert not any("ADD (export_mode" in item[0] for item in connection.executed)
    assert "DEFAULT 'SCHEMA'" in preview_sql(upgrade=True)
    assert "DEFAULT '[]'" in preview_sql(upgrade=True)


def test_scope_upgrade_resumes_master_cleanup_column():
    """Upgrade the previous revision without changing existing task columns or data."""
    from oraexport.migrations import SCOPE_REVISION

    connection = MigrationConnection()
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    connection.tables["runs"].remove("MASTER_CLEANUP_PENDING")
    connection.revisions = [SCOPE_REVISION]
    connection.fail_at = "ALTER TABLE runs ADD"
    with pytest.raises(ConnectionError):
        migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert connection.revisions == [SCOPE_REVISION]
    connection.fail_at = None
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert connection.revisions == [REVISION]
    assert connection.tables["runs"][-1] == "MASTER_CLEANUP_PENDING"
    connection.executed.clear()
    migrate(connection=cast(typ=oracledb.Connection, val=connection))
    assert not any(sql.startswith("ALTER") for sql, _ in connection.executed)
