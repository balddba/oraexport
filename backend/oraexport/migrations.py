"""Versioned Oracle DDL applied directly through python-oracledb."""

import re
from importlib.resources import files

import oracledb

from .config import get_settings
from .oracle_utilities import OracleUtilities

# Revision identifiers for tracking metadata schema version in alembic_version.
LEGACY_REVISION = "7ee62792313d"
SCOPE_REVISION = "20261009_export_scope"
REVISION = "20261015_master_cleanup"
MASTER_COLUMN = "master_cleanup_pending NUMBER(1) DEFAULT 1 NOT NULL CHECK (master_cleanup_pending IN (0, 1))"

# Decomposed column definitions for tasks table migration to support multi-schema and full exports.
m_p1 = "export_mode VARCHAR2(6 CHAR) DEFAULT 'SCHEMA' NOT NULL"
m_p2 = "CHECK (export_mode IN ('SCHEMA', 'FULL'))"
mode_ddl = f"{m_p1} {m_p2}"
names_ddl = "schema_names CLOB DEFAULT '[]' NOT NULL"
col_mode = ("EXPORT_MODE", mode_ddl)
col_names = ("SCHEMA_NAMES", names_ddl)
TASK_COLUMNS = [col_mode, col_names]


def schema_statements() -> list[str]:
    """Load the packaged, reviewable Oracle schema statements.

    Reads the static schema.sql resource bundled with oraexport and parses
    it into individual executable DDL statements.

    Returns:
        list[str]: Cleaned DDL statements for creating metadata tables and indexes.
    """
    # Read the packaged schema.sql resource and split into individual DDL statements.
    raw_sql = files(anchor="oraexport").joinpath("schema.sql").read_text()
    raw_statements = raw_sql.split(";")
    return [stmt.strip() for stmt in raw_statements if stmt.strip()]


def preview_sql(*, upgrade: bool = False) -> str:
    """Return reviewable SQL for a fresh installation or the legacy scope upgrade.

    Args:
        upgrade (bool): True to upgrade legacy schema via ALTER TABLE; False for fresh install.

    Returns:
        str: Complete reviewable SQL script with statements separated by semicolons.
    """
    if upgrade:
        # Build reviewable upgrade statements for legacy installations.
        upgrade_statements = [f"ALTER TABLE tasks ADD ({item[1]})" for item in TASK_COLUMNS]
        u_p1 = f"UPDATE alembic_version SET version_num = '{REVISION}'"
        u_p2 = f"WHERE version_num = '{LEGACY_REVISION}'"
        stmt_update = f"{u_p1} {u_p2}"
        upgrade_statements.append(f"ALTER TABLE runs ADD ({MASTER_COLUMN})")
        upgrade_statements.append(stmt_update)
        upgrade_statements.append("COMMIT")
        return ";\n\n".join(upgrade_statements) + ";\n"
    # Build complete fresh installation statements with alembic_version and initial worker lease.
    stmt_ver = "CREATE TABLE alembic_version (version_num VARCHAR2(32 CHAR) NOT NULL PRIMARY KEY)"
    l_p1 = "INSERT INTO worker_leases (name, owner, expires_at)"
    l_p2 = "VALUES ('scheduler', 'unassigned', TIMESTAMP '1970-01-01 00:00:00')"
    stmt_lease = f"{l_p1} {l_p2}"
    stmt_rev = f"INSERT INTO alembic_version (version_num) VALUES ('{REVISION}')"
    statements = [stmt_ver]
    statements.extend(schema_statements())
    statements.append(stmt_lease)
    statements.append(stmt_rev)
    statements.append("COMMIT")
    return ";\n\n".join(statements) + ";\n"


def migrate(connection: oracledb.Connection) -> None:
    """Install the schema, resume interrupted DDL, or upgrade the existing legacy revision.

    Oracle DDL commits independently. The API runs this during startup before
    accepting requests; standalone workers must wait for migration completion. The existing revision table is retained for installations created by
    the previous migration tool; no data or credentials are rewritten.

    Args:
        connection (oracledb.Connection): Active Oracle database connection with DDL privileges.

    Raises:
        RuntimeError: If the database contains an unknown revision or mismatched schema.
    """
    with connection.cursor() as cursor:
        # Enumerate existing tables in user schema to ensure idempotent table creation.
        cursor.execute("SELECT table_name FROM user_tables")
        tables = {row[0].lower() for row in cursor.fetchall()}
        # Create alembic_version table if it does not yet exist.
        if "alembic_version" not in tables:
            cursor.execute("CREATE TABLE alembic_version (version_num VARCHAR2(32 CHAR) NOT NULL PRIMARY KEY)")
        # Verify existing migration version; stop immediately if unrecognized revision is found.
        cursor.execute("SELECT version_num FROM alembic_version")
        revisions = [row[0] for row in cursor.fetchall()]
        if revisions and revisions not in ([REVISION], [SCOPE_REVISION], [LEGACY_REVISION]):
            raise RuntimeError("Unknown metadata schema revision; migration stopped")
        # Enumerate existing indexes to avoid duplicating pre-existing indexes.
        cursor.execute("SELECT index_name FROM user_indexes")
        indexes = {row[0].lower() for row in cursor.fetchall()}
        # Iterate over package schema statements, validating structure before creation.
        for statement in schema_statements():
            match_obj = re.match(pattern=r"CREATE (TABLE|INDEX) (\w+)", string=statement)
            if match_obj is None:
                raise RuntimeError("Unsupported schema statement")
            groups = match_obj.groups()
            kind = groups[0]
            name = groups[1]
            if kind == "TABLE" and name in tables:
                # Check the complete column list before adopting an existing installation.
                stmt_lines = [line.strip() for line in statement.splitlines()[1:-1]]
                filter_prefixes = ("PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT")
                col_lines = [line for line in stmt_lines if not line.startswith(filter_prefixes)]
                expected = [line.split()[0].upper() for line in col_lines]
                cursor.execute(
                    ("SELECT column_name FROM user_tab_columns WHERE table_name = :table_name ORDER BY column_id"),
                    {"table_name": name.upper()},
                )
                actual = [row[0] for row in cursor.fetchall()]
                # If tasks table exists from legacy revision, perform inline column upgrade.
                is_tasks = name == "tasks"
                needs_upgrade = revisions != [REVISION]
                prefix_match = actual == expected[: len(actual)]
                col_count_ok = len(expected) - 2 <= len(actual) < len(expected)
                if is_tasks and needs_upgrade and prefix_match and col_count_ok:
                    for col_item in TASK_COLUMNS:
                        column = col_item[0]
                        definition = col_item[1]
                        if column not in actual:
                            cursor.execute(f"ALTER TABLE tasks ADD ({definition})")
                            actual.append(column)
                if name == "runs" and revisions != [REVISION] and actual == expected[:-1]:
                    cursor.execute(f"ALTER TABLE runs ADD ({MASTER_COLUMN})")
                    actual.append("MASTER_CLEANUP_PENDING")
                if actual != expected:
                    raise RuntimeError(f"Metadata table {name} does not match the current schema")
            elif kind == "INDEX" and name in indexes:
                continue
            else:
                if revisions:
                    raise RuntimeError(f"Metadata schema is missing {kind.lower()} {name}")
                # Execute the DDL statement; Oracle DDL commits implicitly.
                cursor.execute(statement)
        # Ensure a singleton scheduler lease row exists with expired timestamp for initial acquire.
        cursor.execute(("INSERT INTO worker_leases (name, owner, expires_at) SELECT 'scheduler', 'unassigned', TIMESTAMP '1970-01-01 00:00:00' FROM dual WHERE NOT EXISTS (SELECT 1 FROM worker_leases WHERE name = 'scheduler')"))
        # Update or insert revision record in alembic_version table.
        if not revisions:
            cursor.execute(
                ("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": REVISION},
            )
        if revisions in ([LEGACY_REVISION], [SCOPE_REVISION]):
            cursor.execute("UPDATE alembic_version SET version_num = :revision", {"revision": REVISION})
        # Explicit commit for metadata DML inserts/updates.
        connection.commit()


def migrate_configured_database() -> None:
    """Connect to the configured Oracle schema and apply the current DDL revision.

    Loads connection parameters from application settings, connects to the
    metadata database, and executes schema migration.

    Raises:
        RuntimeError: If schema verification fails or an unknown revision is encountered.
    """
    raw_url = get_settings().metadata_url.get_secret_value()
    params = OracleUtilities.connection_parameters(url=raw_url)
    with oracledb.connect(user=params.user, password=params.password, dsn=params.dsn, tcp_connect_timeout=params.tcp_connect_timeout) as connection:
        migrate(connection=connection)
