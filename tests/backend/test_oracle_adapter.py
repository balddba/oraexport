"""Oracle adapter tests with cursor contracts and realistic catalog values."""

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock

import oracledb
import pytest
from dbapi import execute, fetch_records

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.oracle_unavailable import OracleUnavailable
from oraexport.exceptions.safe_error import safe_error
from oraexport.models.credential import Credential
from oraexport.models.directory_info import DirectoryInfo
from oraexport.models.discovered_pdb import DiscoveredPdb
from oraexport.models.discovery_response import DiscoveryResponse
from oraexport.models.provision_input import ProvisionInput
from oraexport.models.target_catalog_response import TargetCatalogResponse
from oraexport.models.task_input import TaskInput
from oraexport.oracle_database import OracleDatabase
from oraexport.oracle_utilities import OracleUtilities


@pytest.mark.parametrize("failure", [None, "setup", "operation"])
def test_connect_yields_raw_driver_and_closes_on_failure(monkeypatch, failure):
    """Authentication and error translation retain deterministic driver cleanup."""
    driver = MagicMock()
    open_driver = MagicMock(return_value=driver)
    monkeypatch.setattr(oracledb, "connect", open_driver)
    credential = Credential(username="TEST_ADMIN", ciphertext=EncryptionSerivce.encrypt("test-password"), sysdba=True)
    error = oracledb.DatabaseError("ORA-03113: test-only connection details")
    if failure == "setup":
        driver.cursor.return_value.__enter__.return_value.execute.side_effect = error

    def operation():
        """Exercise the connection boundary with an optional remote Oracle error."""
        with OracleUtilities.connect("test:1521/service", credential) as connection:
            assert connection is driver
            assert connection.call_timeout == 20000
            if failure == "operation":
                raise error

    if failure is None:
        operation()
    else:
        with pytest.raises(OracleUnavailable, match=r"^ORA-03113$"):
            operation()
    driver.close.assert_called_once()
    assert open_driver.call_args.kwargs == {
        "user": "TEST_ADMIN",
        "password": "test-password",
        "dsn": "test:1521/service",
        "mode": oracledb.AUTH_MODE_SYSDBA,
        "tcp_connect_timeout": 10,
    }


@pytest.fixture
def transport(db):
    """Build connected databases around cursor doubles and close them after each test.

    Args:
        db (Connection): Caller-owned metadata session.

    Yields:
        Callable: Factory returning a connected database and its cursor double.
    """
    with ExitStack() as stack:

        def build():
            """Open a database's capabilities on a non-network driver double."""
            connection = MagicMock()
            connection.version = "19.23.0.0.0"

            @contextmanager
            def connection_factory(dsn, credential):
                """Yield the driver double for the connected context."""
                yield connection

            database = OracleDatabase("adapter-test", db, connection_factory=connection_factory)
            stack.enter_context(database.connected("test:1521/service", MagicMock()))
            return database, connection.cursor.return_value

        yield build


@pytest.mark.parametrize(
    ("cdb", "container", "kind"),
    [("YES", "CDB$ROOT", "cdb"), ("YES", "FINANCE", "pdb"), ("NO", None, "non_cdb")],
)
def test_detect_container(cdb, container, kind, transport):
    """Detect the connected container instead of trusting user-supplied topology.

    Args:
        cdb (str): 'YES' or 'NO' flag from v$database.cdb.
        container (str | None): Container name from sys_context.
        kind (str): Expected discovered database architecture ('cdb', 'pdb', 'non_cdb').
        transport (Callable): Factory for a database with a mocked Oracle connection.
    """
    # Decompose transport pair unpacking into single lines
    t = transport()
    oracle = t[0]
    cursor = t[1]
    # Seed cursor fetch responses for cdb check, container name, and banner
    cursor.fetchone.side_effect = [(cdb,), (container,), ("Oracle Enterprise Edition",)]
    cursor.__iter__.return_value = [("FINANCE", "READ WRITE")]
    result = oracle.discover()
    assert isinstance(result, DiscoveryResponse)
    assert result.kind == kind


def test_discover_pdb_services(transport):
    """Associate actual network services with their PDB, excluding unrelated services."""
    # Decompose transport pair unpacking into single lines
    t = transport()
    oracle = t[0]
    cursor = t[1]
    cursor.fetchone.side_effect = [("YES",), ("CDB$ROOT",), ("Oracle Enterprise Edition",)]
    # Decompose multi-line iterators into single-line lists
    pdb_rows = [("FINANCE", "READ WRITE"), ("HR", "MOUNTED")]
    svc_rows = [("FINANCE", "finance.example"), ("FINANCE", "finance_reports.example")]
    svc_rows.extend([("FINANCE", "finance.example"), ("CDB$ROOT", "root.example")])
    cursor.__iter__.side_effect = [iter(pdb_rows), iter(svc_rows)]
    # Verify discovered PDB objects and deduplicated service names
    discovered = oracle.discover()
    assert discovered.pdbs == [
        DiscoveredPdb(name="FINANCE", open_mode="READ WRITE", services=["finance.example", "finance_reports.example"]),
        DiscoveredPdb(name="HR", open_mode="MOUNTED"),
    ]


@pytest.mark.parametrize("code", [942, 1031])
def test_discover_without_service_privileges(code, transport):
    """PDB discovery remains usable when the service view is unavailable.

    Args:
        code (int): Oracle error code (e.g. 942 for missing table, 1031 for no privileges).
        transport (Callable): Factory for a database with a mocked Oracle connection.
    """
    t = transport()
    oracle = t[0]
    cursor = t[1]
    cursor.fetchone.side_effect = [("YES",), ("CDB$ROOT",), ("Oracle Enterprise Edition",)]
    cursor.__iter__.return_value = [("FINANCE", "READ WRITE")]
    # Decompose side effect list with simulated privilege error
    err_side_effect = [None, None, None, None]
    err_side_effect.append(oracledb.DatabaseError(MagicMock(code=code)))
    cursor.execute.side_effect = err_side_effect
    assert oracle.discover().pdbs[0].services == []


@pytest.mark.parametrize(
    "dsn",
    [
        "db.example:1522/root.example",
        "tcps://db.example:1522/root.example?wallet_location=/tmp/wallet",
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db.example)(PORT=1522))(CONNECT_DATA=(SERVICE_NAME=root.example)))",
    ],
)
def test_suggested_parse_dsn_preserves_listener(dsn):
    """Replace only the service, preserving the registered host, port, and transport.

    Args:
        dsn (str): Original target connection string.
    """
    original = oracledb.ConnectParams()
    original.parse_connect_string(dsn)
    suggested = OracleUtilities.parse_dsn(dsn, "finance.example")
    assert suggested is not None
    params = oracledb.ConnectParams()
    params.parse_connect_string(suggested)
    assert params.service_name == "finance.example"
    # Preserved network connection parameters
    assert params.host == original.host
    assert params.port == original.port
    assert params.protocol == original.protocol
    assert params.wallet_location == original.wallet_location


def test_parse_dsn_manual_fallback():
    """Unresolvable aliases and SID connections require an explicit target DSN."""
    assert OracleUtilities.parse_dsn("missing-oraexport-alias", "finance") is None
    sid_desc = "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db)(PORT=1521))(CONNECT_DATA=(SID=ROOT)))"
    assert OracleUtilities.parse_dsn(sid_desc, "finance") is None


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("db.example:1522/root", "db.example:1522/finance.example"),
        (
            "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db.example)(PORT=1522))(CONNECT_DATA=(SERVICE_NAME=root)))",
            "db.example:1522/finance.example",
        ),
        (
            "tcps://db.example:1522/root?wallet_location=/tmp/wallet",
            "tcps://db.example:1522/finance.example?wallet_location=/tmp/wallet",
        ),
    ],
)
def test_parse_dsn_prefers_easy_connect(dsn, expected):
    """Simple listeners and existing Easy Connect options keep a compact suggestion.

    Args:
        dsn (str): Input connection string.
        expected (str): Expected canonical suggested DSN.
    """
    assert OracleUtilities.parse_dsn(dsn, "finance.example") == expected


@pytest.mark.parametrize(
    "dsn",
    [
        "(DESCRIPTION=(RETRY_COUNT=3)(ADDRESS=(PROTOCOL=TCP)(HOST=db)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=root)))",
        "(DESCRIPTION=(ADDRESS_LIST=(ADDRESS=(PROTOCOL=TCP)(HOST=db1)(PORT=1521))(ADDRESS=(PROTOCOL=TCP)(HOST=db2)(PORT=1522)))(CONNECT_DATA=(SERVICE_NAME=root)))",
    ],
)
def test_parse_dsn_retains_complex_descriptor(dsn):
    """Settings that cannot fit the simple suggestion retain the complete descriptor.

    Args:
        dsn (str): Complex TNS descriptor connect string.
    """
    original = oracledb.ConnectParams()
    original.parse_connect_string(dsn)
    original.set(service_name="finance.example")
    assert OracleUtilities.parse_dsn(dsn, "finance.example") == original.get_connect_string()


def test_provision_idempotent_and_refuse_unowned(transport):
    """Only app-owned existing users may receive repeated grants; passwords never reset."""
    t = transport()
    oracle = t[0]
    cursor = t[1]
    # Mock catalog metadata response
    catalog_ret = TargetCatalogResponse(kind="pdb", container_name="FINANCE", version="19c", edition="Enterprise", directories=[DirectoryInfo(name="EXPORTS", path="/var/exports")], tablespaces=["USERS"])
    oracle.discover_catalog = MagicMock(return_value=catalog_ret)
    options = ProvisionInput(directory="EXPORTS")
    cursor.fetchone.return_value = (0,)
    oracle.provision_export_account(options, "Oe_" + "a" * 48)
    assert any("CREATE USER" in call.args[0] for call in cursor.execute.call_args_list)
    cursor.reset_mock()
    # Provisioning when user already exists must grant permissions without recreating user
    cursor.fetchone.return_value = (1,)
    oracle.provision_export_account(options, None)
    assert not any("CREATE USER" in call.args[0] for call in cursor.execute.call_args_list)
    assert any("GRANT READ, WRITE" in call.args[0] for call in cursor.execute.call_args_list)
    with pytest.raises(ValueError, match="already exists"):
        oracle.provision_export_account(options, "Oe_" + "a" * 48)


def test_parallel_and_snapshot_capabilities(transport):
    """Reject unsupported parallelism and missing consistent-snapshot privileges."""
    t = transport()
    oracle = t[0]
    cursor = t[1]
    cursor.fetchone.side_effect = [(1,), ("Oracle Standard Edition",)]
    cursor.__iter__.return_value = [("DATAPUMP_EXP_FULL_DATABASE",)]
    task = TaskInput(name="Test", target_id="target", schema_name="HR", directory="EXPORTS", parallel=2)
    # Standard edition does not support parallel Data Pump
    with pytest.raises(ValueError, match="Enterprise"):
        oracle.verify_export_task(task)
    # Second transport check for consistent snapshot privilege
    t2 = transport()
    oracle = t2[0]
    cursor = t2[1]
    cursor.fetchone.return_value = (1,)
    priv_rows = [iter([("DATAPUMP_EXP_FULL_DATABASE",)]), iter([("CREATE SESSION",)])]
    cursor.__iter__.side_effect = priv_rows
    with pytest.raises(ValueError, match="FLASHBACK"):
        oracle.verify_export_task(task.model_copy(update={"parallel": 1, "consistent_snapshot": True}))


def test_error_sanitization():
    """Expose error codes without connection strings or credential fragments."""
    # Redact credentials and hosts, leaving only ORA- error codes
    assert safe_error(RuntimeError("ORA-01017 password=my-secret @host")) == "ORA-01017"
    assert safe_error(RuntimeError("private contents")) == "RuntimeError"


@pytest.mark.parametrize(
    ("scope", "mode", "expression"),
    [
        ({"schema_name": "HR"}, "SCHEMA", "IN ('HR')"),
        ({"schema_names": ["HR", "FINANCE"]}, "SCHEMA", "IN ('HR', 'FINANCE')"),
        ({"export_mode": "FULL", "schema_names": []}, "FULL", None),
    ],
)
def test_start_export_scope(scope, mode, expression, transport):
    """Bind the Data Pump job mode and complete schema filter, including old snapshots.

    Args:
        scope (dict[str, Any]): Export scope schema payload.
        mode (str): Expected export mode string ('SCHEMA' or 'FULL').
        expression (str | None): Expected SQL schema expression filter.
        transport (Callable): Factory for a database with a mocked Oracle connection.
    """
    t = transport()
    oracle = t[0]
    cursor = t[1]
    start_options: dict[str, object] = {"directory": "EXPORTS", "file_size_gb": 10, "parallel": 1}
    start_options["content"] = "ALL"
    start_options["consistent_snapshot"] = False
    start_options.update(scope)
    oracle.start_datapump_job("OE_TEST", "test", start_options)
    sql = cursor.execute.call_args.args[0]
    binds = cursor.execute.call_args.kwargs
    assert binds["export_mode"] == mode
    assert binds["schema_expr"] == expression
    assert "IF :export_mode = 'SCHEMA' THEN" in sql
    assert "OPEN('EXPORT', :export_mode" in sql
    assert binds["dump"] == "oe_test_%L.dmp"
    assert binds["logfile"] == "oe_test.log"


def test_start_uses_saved_readable_filenames(transport):
    """Send the immutable readable dump template and matching log name to Oracle."""
    t = transport()
    oracle = t[0]
    cursor = t[1]
    prefix = "2026-10-12_143025_PRODDB_SALES_HR+PAYROLL_a7c92e"
    start_options: dict[str, object] = {"directory": "EXPORTS", "file_size_gb": 10, "parallel": 1}
    start_options["content"] = "ALL"
    start_options["consistent_snapshot"] = False
    start_options["schema_names"] = ["HR", "PAYROLL"]
    start_options["file_prefix"] = prefix
    oracle.start_datapump_job("OE_TEST", "a7c92e12345678901234567890123456", start_options)
    binds = cursor.execute.call_args.kwargs
    assert binds["dump"] == prefix + "_%L.dmp"
    assert binds["logfile"] == prefix + ".log"


@pytest.mark.parametrize(
    ("method", "args"),
    [
        (
            "start_datapump_job",
            (
                "OE_TEST",
                "test",
                {
                    "schema_names": ["HR"],
                    "directory": "EXPORTS",
                    "file_size_gb": 10,
                    "parallel": 1,
                    "content": "ALL",
                    "consistent_snapshot": False,
                },
            ),
        ),
        ("datapump_job_status", ("OE_TEST",)),
        ("stop_datapump_job", ("OE_TEST",)),
        ("verify_datapump_inactive", ("OE_TEST",)),
        ("release_datapump_master", ("OE_" + "A" * 27,)),
    ],
)
def test_datapump_operations_require_connected_session(db, method, args):
    """Data Pump operations require the database's authenticated Oracle session."""
    database = OracleDatabase("datapump-test", db)
    with pytest.raises(RuntimeError, match="connected"):
        getattr(database, method)(*args)


@pytest.mark.parametrize(("progress", "expected"), [(None, None), (42.9, 42)])
def test_datapump_status_parses_driver_values(transport, progress, expected):
    """Status polling converts scalar and CLOB results while preserving unknown progress."""
    database, cursor = transport()
    values = ["EXECUTING", progress, 1, "Exporting HR\n", "ORA-39100\n", "hr_01.dmp\n"]
    variables = [MagicMock() for _ in values]
    for variable, value in zip(variables, values, strict=True):
        variable.getvalue.return_value = value
    cursor.var.side_effect = variables
    status = database.datapump_job_status("OE_TEST")
    assert status.state == "EXECUTING"
    assert status.progress == expected
    assert status.error_count == 1
    assert status.messages == ["Exporting HR"]
    assert status.errors == ["ORA-39100"]
    assert status.files == ["hr_01.dmp"]
    assert cursor.execute.call_args.kwargs["job"] == "OE_TEST"


def test_datapump_stop_preserves_master_table(transport):
    """Cancellation stops the selected job immediately and keeps recovery metadata."""
    database, cursor = transport()
    database.stop_datapump_job("OE_TEST")
    sql = cursor.execute.call_args.args[0]
    assert "DBMS_DATAPUMP.ATTACH(:job)" in sql
    assert "STOP_JOB(h, immediate => 1, keep_master => 1)" in sql
    assert cursor.execute.call_args.kwargs == {"job": "OE_TEST"}


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([None], None),
        ([("STOPPED",), (0,)], None),
        ([("EXECUTING",)], "active job"),
        ([("DEFINING",), (1,)], "attached sessions"),
    ],
)
def test_datapump_inactivity_checks_state_and_sessions(transport, rows, error):
    """Inactive jobs allow cleanup while running jobs and attached clients block it."""
    database, cursor = transport()
    cursor.fetchone.side_effect = rows
    if error:
        with pytest.raises(ValueError, match=error):
            database.verify_datapump_inactive("OE_TEST")
    else:
        database.verify_datapump_inactive("OE_TEST")


@pytest.mark.parametrize("exists", [False, True])
def test_datapump_master_cleanup_checks_inactivity_before_drop(transport, exists):
    """Master cleanup only drops an existing table after confirming job inactivity."""
    database, cursor = transport()
    job = "OE_" + "A" * 27
    cursor.fetchone.side_effect = [("STOPPED",), (0,), (int(exists),)]
    database.release_datapump_master(job)
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert "user_datapump_jobs" in statements[0]
    assert "dba_datapump_sessions" in statements[1]
    assert "user_tables" in statements[2]
    assert statements[3:] == ([f"DROP TABLE {job} PURGE"] if exists else [])


def test_datapump_master_cleanup_rejects_foreign_or_active_jobs(transport):
    """Foreign identifiers and active jobs never reach master-table deletion."""
    database, cursor = transport()
    with pytest.raises(ValueError, match="namespace"):
        database.release_datapump_master("OTHER_TABLE")
    cursor.execute.assert_not_called()
    cursor.fetchone.return_value = ("EXECUTING",)
    with pytest.raises(ValueError, match="active job"):
        database.release_datapump_master("OE_" + "A" * 27)
    assert cursor.execute.call_count == 1


def test_verify_all_selected_schemas_and_full_privileges(transport):
    """Validate every selected schema and require export privileges in full mode too."""
    t = transport()
    oracle = t[0]
    cursor = t[1]
    task = TaskInput(name="Multiple", target_id="t", directory="EXPORTS", schema_names=["HR", "FINANCE"])
    cursor.fetchone.side_effect = [(1,), (0,)]
    # Unrecognized schema triggers validation error
    with pytest.raises(ValueError, match="FINANCE"):
        oracle.verify_export_task(task)
    assert [call.kwargs["name"] for call in cursor.execute.call_args_list] == ["HR", "FINANCE"]
    t2 = transport()
    oracle = t2[0]
    cursor = t2[1]
    cursor.__iter__.return_value = []
    full = TaskInput(name="Full", target_id="t", directory="EXPORTS", export_mode="FULL")
    # Full database export requires DATAPUMP_EXP_FULL_DATABASE role
    with pytest.raises(ValueError, match="DATAPUMP_EXP_FULL_DATABASE"):
        oracle.verify_export_task(full)
    assert not any("all_users" in call.args[0] for call in cursor.execute.call_args_list)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("verify_directory", ("EXPORTS",)),
        ("read_log", ("EXPORTS", "export.log")),
        ("file_size", ("EXPORTS", "export.dmp")),
        ("remove_file", ("EXPORTS", "export.dmp")),
    ],
)
def test_database_file_operations_require_connected_session(db, method, args):
    """File operations reject use without an authenticated Oracle session."""
    database = OracleDatabase("file-test", db)
    with pytest.raises(RuntimeError, match="connected"):
        getattr(database, method)(*args)


def test_database_verify_directory_uses_unique_probe_and_validates_identifier(transport):
    """Directory probes use normalized binds and reject invalid directory identifiers."""
    database, cursor = transport()
    database.verify_directory("exports")
    first = cursor.execute.call_args.kwargs
    database.verify_directory("EXPORTS")
    second = cursor.execute.call_args.kwargs
    assert first["directory"] == second["directory"] == "EXPORTS"
    assert first["filename"].startswith("oraexport_probe_")
    assert first["filename"].endswith(".tmp")
    assert first["filename"] != second["filename"]
    cursor.execute.reset_mock()
    with pytest.raises(ValueError, match="Use an unquoted Oracle identifier"):
        database.verify_directory("invalid/directory")
    cursor.execute.assert_not_called()


@pytest.mark.parametrize("use_locator", [False, True])
def test_database_read_log_normalizes_clob(transport, use_locator):
    """Log reads normalize both driver strings and LOB locators through the shared cursor."""
    database, cursor = transport()
    content = "Export started\n\nExport completed\n"
    if use_locator:
        locator = MagicMock()
        locator.read.return_value = content
        cursor.var.return_value.getvalue.return_value = locator
    else:
        cursor.var.return_value.getvalue.return_value = content
    assert database.read_log("EXPORTS", "export.log") == ["Export started", "", "Export completed"]
    cursor.var.assert_called_once_with(oracledb.DB_TYPE_CLOB)
    assert cursor.execute.call_args.kwargs == {
        "directory": "EXPORTS",
        "filename": "export.log",
        "output": cursor.var.return_value,
    }


@pytest.mark.parametrize("size", [None, 0, 1024])
def test_database_file_size_and_idempotent_removal(transport, size):
    """Absent files return None; existing files, including empty files, are removed."""
    database, cursor = transport()
    cursor.var.return_value.getvalue.return_value = size
    assert database.file_size("EXPORTS", "export.dmp") == size
    cursor.var.assert_called_once_with(oracledb.NUMBER)
    database.remove_file("EXPORTS", "export.dmp")
    if size is None:
        cursor.callproc.assert_not_called()
    else:
        cursor.callproc.assert_called_once_with(
            name="UTL_FILE.FREMOVE",
            keyword_parameters={"location": "EXPORTS", "filename": "export.dmp"},
        )


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("verify_directory", ("EXPORTS",)),
        ("read_log", ("EXPORTS", "export.log")),
        ("file_size", ("EXPORTS", "export.dmp")),
        ("remove_file", ("EXPORTS", "export.dmp")),
    ],
)
def test_database_file_operations_propagate_oracle_errors(transport, method, args):
    """File SQL failures remain visible to the database workflow's recovery handling."""
    database, cursor = transport()
    cursor.execute.side_effect = oracledb.DatabaseError("ORA-29283")
    with pytest.raises(oracledb.DatabaseError, match="ORA-29283"):
        getattr(database, method)(*args)
    cursor.callproc.assert_not_called()


def test_discover_catalog_filters_asm_directories(transport):
    """Catalog discovery queries the connected PDB and returns filesystem destinations."""
    oracle, cursor = transport()
    cursor.fetchone.side_effect = [("YES",), ("FINANCE",), ("Oracle Enterprise Edition",)]
    cursor.__iter__.side_effect = [
        iter([("FINANCE",), ("HR",)]),
        iter([("EXPORTS", "/var/exports"), ("ASM", "+DATA")]),
        iter([("USERS",), ("EXPORT_DATA",)]),
    ]
    catalog = oracle.discover_catalog()
    assert isinstance(catalog, TargetCatalogResponse)
    assert catalog.kind == "pdb"
    assert catalog.schemas == ["FINANCE", "HR"]
    assert catalog.directories == [DirectoryInfo(name="EXPORTS", path="/var/exports")]
    assert catalog.tablespaces == ["USERS", "EXPORT_DATA"]


@pytest.mark.parametrize("container", ["CDB$ROOT", "PDB$SEED"])
def test_discover_catalog_rejects_root_and_seed(container, transport):
    """Only directly connected export services are eligible for catalogs."""
    oracle, cursor = transport()
    cursor.fetchone.side_effect = [("YES",), (container,), ("Oracle Enterprise Edition",)]
    cursor.__iter__.return_value = []
    with pytest.raises(ValueError, match="Connect directly"):
        oracle.discover_catalog()
    assert not any("dba_users" in call.args[0] for call in cursor.execute.call_args_list)


def test_discover_rejects_unsupported_oracle_version(transport):
    """Reject discovery against Oracle versions older than 19c.

    Args:
        transport (Callable): Factory for a database with a mocked Oracle connection.
    """
    oracle, cursor = transport()
    oracle.oracle_connection.version = "18.0.0.0.0"
    cursor.fetchone.side_effect = [("NO",), (None,), ("Oracle Enterprise Edition",)]
    with pytest.raises(ValueError, match="19c or later"):
        oracle.discover()


@pytest.mark.parametrize("method", ["discover", "discover_catalog"])
def test_discovery_requires_connected_context(db, method):
    """Require an active Oracle connection before discovering topology or catalogs.

    Args:
        db (Connection): Isolated metadata database session.
        method (str): Discovery method to invoke outside a connected context.
    """
    database = OracleDatabase(None, db)
    with pytest.raises(RuntimeError, match="connected"):
        getattr(database, method)()


def test_discovery_database_requires_target_for_owned_workflows(db):
    """Reject target workflows on a database created solely for discovery.

    Args:
        db (Connection): Isolated metadata database session.
    """
    database = OracleDatabase(None, db)
    with pytest.raises(RuntimeError, match="not bound to an export target"):
        database.target()


@pytest.mark.parametrize("consistent_snapshot", [False, True])
@pytest.mark.parametrize("reuse_account", [False, True])
def test_provisioning_execution_matches_preview(db, task, transport, consistent_snapshot, reuse_account):
    """Review and execution agree on all grants and mask generated passwords."""
    from oraexport.models.target import Target
    from oraexport.models.user import User

    target = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": task.target_id},
            )
        ),
        None,
    )
    assert target is not None
    if not reuse_account:
        target.export_credential_id = None
        execute(
            db,
            ("UPDATE targets SET export_credential_id = :export_credential_id WHERE id = :record_key"),
            {"export_credential_id": target.export_credential_id, "record_key": target.id},
        )
    user = next(
        iter(
            fetch_records(
                db,
                User,
                ("SELECT id, username, password_hash, role, enabled FROM app_users WHERE username = :p0 FETCH NEXT :row_limit ROWS ONLY"),
                dict({"p0": "admin"}, row_limit=1),
            )
        ),
        None,
    )
    options = ProvisionInput(directory="EXPORTS", quota_mb=256, consistent_snapshot=consistent_snapshot)
    oracle, cursor = transport()
    oracle.target_id = task.target_id
    preview = oracle.provision_preview(options, user)
    oracle.discover_catalog = MagicMock(
        return_value=TargetCatalogResponse(
            kind="pdb",
            container_name="FINANCE",
            version="19c",
            edition="Enterprise",
            directories=[DirectoryInfo(name="EXPORTS", path="/var/exports")],
            tablespaces=["USERS"],
        )
    )
    cursor.fetchone.return_value = (int(reuse_account),)
    password = None if reuse_account else "Oe_" + "a" * 48
    oracle.provision_export_account(options, password)
    executed = [call.args[0] for call in cursor.execute.call_args_list if not call.args[0].startswith("SELECT")]
    if password:
        executed = [statement.replace(password, "<generated password>") for statement in executed]
    assert [statement + ";" for statement in executed] == preview.sql
    has_flashback = any("FLASHBACK ANY TABLE" in statement for statement in executed)
    assert has_flashback is consistent_snapshot
