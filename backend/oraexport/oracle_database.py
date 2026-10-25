"""OracleDatabase-owned task, provisioning, export, and retention workflows."""

import json
import re
import secrets
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import PureWindowsPath
from typing import Self
from zoneinfo import ZoneInfo

import oracledb
from croniter import croniter
from oracledb import Connection

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.application_error import ApplicationError
from oraexport.exceptions.oracle_unavailable import OracleUnavailable
from oraexport.exceptions.safe_error import safe_error
from oraexport.models.backup_file import BackupFile
from oraexport.models.credential import Credential
from oraexport.models.defaults import now, uid
from oraexport.models.directory_info import DirectoryInfo
from oraexport.models.discovered_pdb import DiscoveredPdb
from oraexport.models.discovery_response import DiscoveryResponse
from oraexport.models.event_details import EventDetails
from oraexport.models.job_status import JobStatus
from oraexport.models.provision_input import ProvisionInput
from oraexport.models.provision_preview_response import ProvisionPreviewResponse
from oraexport.models.queued_run_snapshot import QueuedRunSnapshot
from oraexport.models.reconcile_input import ReconcileInput
from oraexport.models.registration import Registration
from oraexport.models.run import Run
from oraexport.models.run_snapshot import RunSnapshot
from oraexport.models.run_state import ACTIVE, TERMINAL
from oraexport.models.target import Target
from oraexport.models.target_catalog_response import TargetCatalogResponse
from oraexport.models.task import Task
from oraexport.models.task_input import TaskInput
from oraexport.models.user import User
from oraexport.models.validators import identifier
from oraexport.oracle_utilities import OracleUtilities
from oraexport.types import OracleConnectionFactory

from .metadata_service import MetadataService
from .notifications import NotificationService


class OracleDatabase:
    """Own workflows for one directly connectable PDB or non-container database.

    Discovery also supports connections without a saved target.
    The metadata session belongs to the caller. Remote capabilities are properties
    bound only inside connected() contexts;
    existing exports always use their captured DSN and credential rather than the
    database's current configuration.
    """

    # Session lifecycle and remote capabilities

    def __init__(
        self,
        target_id: str | None,
        session: Connection,
        connection_factory: OracleConnectionFactory | None = None,
        clock: Callable[[], datetime] = now,
        id_factory: Callable[[], str] = uid,
    ) -> None:
        """Bind one database to the caller's metadata transaction.

        Args:
            target_id (str | None): Owning target, or None for discovery before registration.
            session (Connection): Caller-owned metadata session.
            connection_factory (OracleConnectionFactory | None): Raw driver connection factory.
            clock (Callable[[], datetime]): Timestamp source.
            id_factory (Callable[[], str]): Export identifier allocator.
        """
        self.target_id = target_id
        self.metadata_session = session
        self.metadata = MetadataService(db=session)
        self.connection_factory = connection_factory or OracleUtilities.connect
        self.clock = clock
        self.id_factory = id_factory
        self._oracle_connection: oracledb.Connection | None = None
        self._oracle_cursor: oracledb.Cursor | None = None

    @contextmanager
    def connected(self, dsn: str, credential: Credential) -> Generator[Self, None, None]:
        """Bind database capabilities to one temporary authenticated Oracle session.

        Args:
            dsn (str): Current target DSN or the DSN captured by an export.
            credential (Credential): Administrator or export credential for this operation.

        Yields:
            OracleDatabase: This database with active remote capabilities.

        Raises:
            RuntimeError: If this database already has an active remote session.
        """
        if self._oracle_connection is not None:
            raise RuntimeError("OracleDatabase already has an active Oracle session")
        with self.connection_factory(dsn, credential) as connection:
            try:
                self._oracle_connection = connection
                self._oracle_cursor = connection.cursor()
                yield self
            finally:
                cursor = self._oracle_cursor
                self._oracle_connection = None
                self._oracle_cursor = None
                if cursor is not None:
                    cursor.close()

    @property
    def oracle_connection(self) -> oracledb.Connection:
        """Return the active remote connection, distinct from the metadata session.

        Returns:
            oracledb.Connection: Active remote driver connection.

        Raises:
            RuntimeError: If accessed outside a connected context.
        """
        if self._oracle_connection is None:
            raise RuntimeError("Use OracleDatabase.connected() before accessing Oracle capabilities")
        return self._oracle_connection

    @property
    def oracle_cursor(self) -> oracledb.Cursor:
        """Return the cursor shared by this database's active Oracle capabilities.

        Returns:
            oracledb.Cursor: Shared remote cursor.

        Raises:
            RuntimeError: If accessed outside a connected context.
        """
        if self._oracle_cursor is None:
            raise RuntimeError("Use OracleDatabase.connected() before accessing Oracle capabilities")
        return self._oracle_cursor

    # Metadata access and ownership

    def target(self, lock: bool = False) -> Target:
        """Load the owning database, optionally holding its row lock.

        Args:
            lock (bool): Whether to hold a metadata row lock.

        Returns:
            Target: The owning database record.
        """
        if self.target_id is None:
            raise RuntimeError("This OracleDatabase is not bound to an export target")
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key" + (" FOR UPDATE" if lock else ""),
                {"record_key": self.target_id},
            )
            rows = [Target(id=row[0], registration_id=row[1], name=row[2], dsn=row[3], admin_credential_id=row[4], export_credential_id=row[5], directory=row[6], tablespace=row[7], provisioned=row[8], edition=row[9], container_name=row[10]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        return rows[0]

    def _require_owner(self, item: Task | Run) -> None:
        """Reject operations on tasks or exports owned by another database.

        Args:
            item (Task | Run): Task or export whose owner is checked.
        """
        if item.target_id != self.target_id:
            raise ApplicationError(status_code=409, detail="Task or export belongs to a different database")

    def _task(self, task_id: str, lock: bool = False) -> Task:
        """Load a task and verify database ownership before making changes.

        Args:
            task_id (str): Identifier of the owned export task.
            lock (bool): Whether to hold a metadata row lock.

        Returns:
            Task: The owned task record.
        """
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key" + (" FOR UPDATE" if lock else ""),
                {"record_key": task_id},
            )
            rows = [
                Task(
                    id=row[0],
                    name=row[1],
                    target_id=row[2],
                    schema_name=row[3],
                    directory=row[4],
                    enabled=row[5],
                    cron=row[6],
                    timezone=row[7],
                    retention_days=row[8],
                    parallel=row[9],
                    file_size_gb=row[10],
                    content=row[11],
                    consistent_snapshot=row[12],
                    next_run_at=row[13],
                    export_mode=row[14],
                    schema_names=row[15].read() if hasattr(row[15], "read") else row[15],
                )
                for row in cursor.fetchall()
            ]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        task = rows[0]
        self._require_owner(item=task)
        return task

    def run(self, run_id: str, lock: bool = False) -> Run:
        """Load an export and verify database ownership before making changes.

        Args:
            run_id (str): Identifier of the owned export.
            lock (bool): Whether to hold a metadata row lock.

        Returns:
            Run: The owned export record.
        """
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key" + (" FOR UPDATE" if lock else ""),
                {"record_key": run_id},
            )
            rows = [
                Run(
                    id=row[0],
                    task_id=row[1],
                    target_id=row[2],
                    occurrence=row[3],
                    snapshot=row[4].read() if hasattr(row[4], "read") else row[4],
                    state=row[5],
                    oracle_job=row[6],
                    created_at=row[7],
                    started_at=row[8],
                    finished_at=row[9],
                    progress=row[10],
                    errors=row[11].read() if hasattr(row[11], "read") else row[11],
                    messages=row[12].read() if hasattr(row[12], "read") else row[12],
                    cancel_requested=row[13],
                    last_seen_at=row[14],
                    notification_created=row[15],
                    master_cleanup_pending=row[16],
                )
                for row in cursor.fetchall()
            ]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        run = rows[0]
        self._require_owner(item=run)
        return run

    def runs(self, *, offset: int = 0, limit: int | None = None) -> list[Run]:
        """Read this database's execution history in reverse creation order.

        Args:
            offset (int): Number of matching records to skip.
            limit (int | None): Maximum number of records to return.

        Returns:
            list[Run]: Export records belonging to this database.
        """
        self.target()
        return self.metadata.runs(target_id=self.target_id, offset=offset, limit=limit)

    def run_count(self) -> int:
        """Count this database's execution history."""
        self.target()
        return self.metadata.run_count(target_id=self.target_id)

    # Discovery and catalog

    def discover(self) -> DiscoveryResponse:
        """Detect database topology and list PDB names without guessing service names.

        Queries v$database, userenv, and v$version to identify container architecture,
        pluggable database inventory, and network services registered in the listener.

        Returns:
            DiscoveryResponse: Typed topology details and discovered PDBs.

        Raises:
            ValueError: If Oracle database version is older than 19c.
        """
        # Determine whether this database instance is a container database (CDB).
        self.oracle_cursor.execute("SELECT cdb FROM v$database")
        cdb = self.oracle_cursor.fetchone()[0] == "YES"
        # Determine the current container name (e.g. CDB$ROOT, PDB$SEED, or PDB name).
        self.oracle_cursor.execute("SELECT SYS_CONTEXT('USERENV','CON_NAME') FROM dual")
        container = self.oracle_cursor.fetchone()[0]
        # Inspect full version banner to identify Enterprise or Standard edition.
        self.oracle_cursor.execute("SELECT banner_full FROM v$version WHERE ROWNUM = 1")
        edition = self.oracle_cursor.fetchone()[0]
        version = self.oracle_connection.version
        # OraExport relies on Oracle 19c+ features (e.g. enhanced Data Pump, PDB architectures).
        major_version = int(version.split(".")[0])
        if major_version < 19:
            raise ValueError("Oracle 19c or later is required")
        # Classify the connection topology based on CDB status and container context.
        kind = "cdb" if cdb and container == "CDB$ROOT" else "pdb" if cdb else "non_cdb"
        pdbs = []
        if kind == "cdb":
            # List pluggable databases excluding PDB$SEED (template container).
            self.oracle_cursor.execute(("SELECT name, open_mode FROM v$pdbs WHERE name <> 'PDB$SEED'"))
            pdbs = [DiscoveredPdb(name=row[0], open_mode=row[1]) for row in self.oracle_cursor]
            try:
                # Query registered listener network services for each pluggable database.
                self.oracle_cursor.execute(("SELECT pdb, network_name FROM cdb_services WHERE pdb IS NOT NULL AND network_name IS NOT NULL ORDER BY network_name"))
                services = list(self.oracle_cursor)
            except oracledb.DatabaseError as exc:
                # ORA-00942 (table/view missing) or ORA-01031 (insufficient privilege) handled.
                if exc.args[0].code not in {942, 1031}:
                    raise
                services = []
            # Match discovered network service names to their respective PDB containers.
            for pdb in pdbs:
                svc_names = {row[1] for row in services if row[0] == pdb.name}
                pdb.services = sorted(svc_names)
        return DiscoveryResponse(kind=kind, container_name=container, version=version, edition=edition, pdbs=pdbs)

    def discover_catalog(self) -> TargetCatalogResponse:
        """List application schemas, filesystem directories, and permanent tablespaces.

        Validates that connection is direct to a PDB or non-CDB service, then queries
        dba_users, dba_directories, and dba_tablespaces.

        Returns:
            TargetCatalogResponse: Typed topology, schemas, directories, and tablespaces.

        Raises:
            ValueError: If connected to CDB$ROOT or PDB$SEED instead of an export service.
        """
        topology = self.discover()
        if topology.kind == "cdb" or topology.container_name == "PDB$SEED":
            raise ValueError("Connect directly to a PDB or non-CDB service")
        # List non-Oracle-maintained schemas eligible for export.
        self.oracle_cursor.execute(("SELECT username FROM dba_users WHERE oracle_maintained = 'N' ORDER BY username"))
        schemas = [row[0] for row in self.oracle_cursor]
        # Query filesystem directories, excluding ASM disk groups indicated by '+' prefix.
        self.oracle_cursor.execute(("SELECT directory_name, directory_path FROM dba_directories"))
        valid_dirs = [r for r in self.oracle_cursor if not r[1].startswith("+")]
        directories = [DirectoryInfo(name=r[0], path=r[1]) for r in valid_dirs]
        # Query permanent tablespaces suitable for dedicated export user default tablespace.
        self.oracle_cursor.execute(("SELECT tablespace_name FROM dba_tablespaces WHERE contents = 'PERMANENT'"))
        tablespaces = [row[0] for row in self.oracle_cursor]
        return TargetCatalogResponse.model_validate(
            obj=topology.model_dump() | {"schemas": schemas, "directories": directories, "tablespaces": tablespaces},
        )

    def catalog(self, user: User) -> TargetCatalogResponse:
        """Discover schemas and destinations using the saved DBA credential.

        Args:
            user (User): Authenticated actor for auditing.

        Returns:
            TargetCatalogResponse: The discovered schemas, directories, and tablespaces.
        """
        target = self.target()
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": target.admin_credential_id},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        admin_cred = rows[0] if rows else None
        if admin_cred is None:
            raise ValueError("Required credential record is missing")
        with self.connected(dsn=target.dsn, credential=admin_cred):
            return self.discover_catalog()

    # Account provisioning

    def provision_preview(self, body: ProvisionInput, user: User) -> ProvisionPreviewResponse:
        """Generate the SQL that administrators review before provisioning.

        Args:
            body (ProvisionInput): Validated workflow input.
            user (User): Authenticated actor for auditing.

        Returns:
            ProvisionPreviewResponse: SQL statements and provisioning notes.
        """
        target = self.target()
        export_user = body.export_username
        sql = [
            f'CREATE USER {export_user} IDENTIFIED BY "<generated password>" DEFAULT TABLESPACE {body.tablespace}',
            f"GRANT CREATE SESSION, CREATE TABLE, DATAPUMP_EXP_FULL_DATABASE TO {export_user}",
            f"ALTER USER {export_user} DEFAULT TABLESPACE {body.tablespace} QUOTA {body.quota_mb}M ON {body.tablespace}",
            f"GRANT READ, WRITE ON DIRECTORY {body.directory} TO {export_user}",
            f"GRANT EXECUTE ON SYS.DBMS_DATAPUMP TO {export_user}",
            f"GRANT EXECUTE ON SYS.UTL_FILE TO {export_user}",
            f"GRANT SELECT ON SYS.V_$VERSION TO {export_user}",
            f"GRANT SELECT ON SYS.DBA_DATAPUMP_SESSIONS TO {export_user}",
        ]
        if body.consistent_snapshot:
            sql.extend(
                [
                    f"GRANT FLASHBACK ANY TABLE TO {export_user}",
                    f"GRANT EXECUTE ON SYS.DBMS_FLASHBACK TO {export_user}",
                ]
            )
        if target.export_credential_id:
            with self.metadata_session.cursor() as cursor:
                cursor.execute(
                    "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                    {"record_key": target.export_credential_id},
                )
                rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
            saved = rows[0] if rows else None
            if saved is None:
                raise ValueError("Required credential record is missing")
            if saved.username != body.export_username:
                raise ValueError("Reprovisioning must retain the saved export account")
            sql = sql[1:]
        formatted_sql = [statement + ";" for statement in sql]
        note = "DDL commits independently in Oracle. Existing owned accounts are reused."
        return ProvisionPreviewResponse(sql=formatted_sql, note=note)

    def provision_apply(self, body: ProvisionInput, user: User) -> Target:
        """Apply the reviewed provisioning definition and verify directory access.

        Args:
            body (ProvisionInput): Validated workflow input.
            user (User): Authenticated actor for auditing.

        Returns:
            Target: The provisioned database record.
        """
        target = self.target(lock=True)
        if target.export_credential_id:
            with self.metadata_session.cursor() as cursor:
                cursor.execute(
                    "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                    {"record_key": target.export_credential_id},
                )
                rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
            credential = rows[0] if rows else None
            if credential is None:
                raise ValueError("Required credential record is missing")
        else:
            credential = None
        if credential and credential.username != body.export_username:
            raise ValueError("Reprovisioning must retain the saved export account")
        password = None
        if not credential:
            with self.metadata_session.cursor() as cursor:
                cursor.execute(
                    "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                    {"record_key": target.admin_credential_id},
                )
                rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
            admin_cred = rows[0] if rows else None
            if admin_cred is None:
                raise ValueError("Required credential record is missing")
            with self.connected(dsn=target.dsn, credential=admin_cred):
                self.discover_catalog()
                if self.account_exists(body.export_username):
                    raise ValueError("Choose a new dedicated username; existing accounts are not adopted")
            password = "Oe_" + secrets.token_hex(nbytes=24)
            credential_cipher = EncryptionSerivce.encrypt(value=password)
            credential = Credential(username=body.export_username, ciphertext=credential_cipher)
            with self.metadata_session.cursor() as cursor:
                cursor.setinputsizes(ciphertext=oracledb.DB_TYPE_CLOB)
                parameters = {
                    "id": credential.id,
                    "username": credential.username,
                    "ciphertext": credential.ciphertext,
                    "sysdba": int(credential.sysdba),
                }
                cursor.execute(
                    ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
                    parameters,
                )

            target.export_credential_id = credential.id
            with self.metadata_session.cursor() as cursor:
                parameters = {
                    "export_credential_id": target.export_credential_id,
                    "record_key": target.id,
                }
                cursor.execute(
                    ("UPDATE targets SET export_credential_id = :export_credential_id WHERE id = :record_key"),
                    parameters,
                )
            # Save credentials before independently committed Oracle DDL.
            self.metadata_session.commit()
        try:
            with self.metadata_session.cursor() as cursor:
                cursor.execute(
                    "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                    {"record_key": target.admin_credential_id},
                )
                rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
            admin_cred = rows[0] if rows else None
            if admin_cred is None:
                raise ValueError("Required credential record is missing")
            with self.connected(dsn=target.dsn, credential=admin_cred):
                exists = self.account_exists(credential.username)
                if exists and password is None:
                    provision_pass = None
                else:
                    provision_pass = EncryptionSerivce.decrypt(value=credential.ciphertext)
                self.provision_export_account(body, provision_pass)
            with self.connected(dsn=target.dsn, credential=credential):
                self.verify_directory(body.directory)
        except Exception as exc:
            outbox_payload = {"target_id": target.id, "error": safe_error(exc=exc)}
            outbox = NotificationService()
            outbox.queue_event(db=self.metadata_session, event="provisioning_failed", details=EventDetails(root=outbox_payload))
            self.metadata.record_audit(user=user, action="provisioning_failed", resource_id=target.id)
            self.metadata_session.commit()
            if isinstance(exc, (ValueError, OracleUnavailable)):
                raise
            raise OracleUnavailable(safe_error(exc=exc)) from None
        target.directory = body.directory
        target.tablespace = body.tablespace
        target.provisioned = True
        with self.metadata_session.cursor() as cursor:
            parameters = {
                "directory": target.directory,
                "tablespace": target.tablespace,
                "provisioned": int(target.provisioned),
                "record_key": target.id,
            }
            cursor.execute(
                ("UPDATE targets SET directory = :directory, tablespace = :tablespace, provisioned = :provisioned WHERE id = :record_key"),
                parameters,
            )
        self.metadata.record_audit(user=user, action="target_provisioned", resource_id=target.id)
        self.metadata_session.commit()
        return target

    def account_exists(self, username: str) -> bool:
        """Check account existence without exposing cursors to application services.

        Args:
            username (str): Oracle database username to check in DBA_USERS.

        Returns:
            bool: True if account exists; False otherwise.
        """
        # Query dba_users count using named bind parameter to check username existence.
        self.oracle_cursor.execute("SELECT COUNT(*) FROM dba_users WHERE username=:name", name=username)
        return bool(self.oracle_cursor.fetchone()[0])

    def provision_export_account(self, options: ProvisionInput, password: str | None) -> None:
        """Create a local export account or reuse an application-owned account.

        Validates selected directory and tablespace against catalog, creates the
        user if absent, and applies the required export privileges.

        Args:
            options (ProvisionInput): Validated provisioning input for dedicated export user.
            password (str | None): Generated password for new account, or None if reusing existing.

        Raises:
            ValueError: If directory/tablespace is invalid, password invalid, or user conflict.
        """
        catalog = self.discover_catalog()
        valid_dir_names = {directory.name for directory in catalog.directories}
        if options.directory not in valid_dir_names:
            raise ValueError("Select an existing filesystem DIRECTORY")
        if options.tablespace not in catalog.tablespaces:
            raise ValueError("Select an existing permanent tablespace")
        user = identifier(value=options.export_username)
        self.oracle_cursor.execute(("SELECT COUNT(*) FROM dba_users WHERE username = :name"), name=user)
        exists = self.oracle_cursor.fetchone()[0]
        if exists and password is not None:
            raise ValueError("Account already exists; choose a new dedicated export username")
        if not exists:
            if password is None:
                raise ValueError("Saved export account was removed; create a new target account")
            # Password generated locally from a fixed safe alphabet; never user-supplied SQL.
            if not re.fullmatch(pattern=r"[A-Za-z0-9_]{32,}", string=password):
                raise ValueError("Invalid generated password")
            # Construct single-line CREATE USER statement within column length limits.
            u_p1 = f'CREATE USER {user} IDENTIFIED BY "{password}"'
            u_p2 = f"DEFAULT TABLESPACE {options.tablespace}"
            create_sql = f"{u_p1} {u_p2}"
            self.oracle_cursor.execute(create_sql)
        # Apply export privileges, quota, and optional flashback grants.
        statements = [
            f"GRANT CREATE SESSION, CREATE TABLE, DATAPUMP_EXP_FULL_DATABASE TO {user}",
            f"ALTER USER {user} DEFAULT TABLESPACE {options.tablespace} QUOTA {options.quota_mb}M ON {options.tablespace}",
            f"GRANT READ, WRITE ON DIRECTORY {options.directory} TO {user}",
            f"GRANT EXECUTE ON SYS.DBMS_DATAPUMP TO {user}",
            f"GRANT EXECUTE ON SYS.UTL_FILE TO {user}",
            f"GRANT SELECT ON SYS.V_$VERSION TO {user}",
            f"GRANT SELECT ON SYS.DBA_DATAPUMP_SESSIONS TO {user}",
        ]
        if options.consistent_snapshot:
            statements.extend(
                [
                    f"GRANT FLASHBACK ANY TABLE TO {user}",
                    f"GRANT EXECUTE ON SYS.DBMS_FLASHBACK TO {user}",
                ]
            )
        for sql in statements:
            self.oracle_cursor.execute(sql)

    # Task configuration and scheduling

    def create_task(self, body: TaskInput, user: User) -> Task:
        """Save a verified export task and calculate its next occurrence.

        Args:
            body (TaskInput): Validated workflow input.
            user (User): Authenticated actor for auditing.

        Returns:
            Task: The persisted task.
        """
        if body.target_id != self.target_id:
            raise ApplicationError(status_code=409, detail="Task belongs to a different database")
        self.validate_target(body=body)
        next_run = OracleDatabase.next_occurrence(expression=body.cron, zone=body.timezone, after=self.clock())
        task = Task(
            name=body.name,
            target_id=body.target_id,
            schema_name=body.schema_name or "FULL",
            schema_names=json.dumps(obj=body.schema_names),
            export_mode=body.export_mode,
            directory=body.directory,
            enabled=body.enabled,
            cron=body.cron,
            timezone=body.timezone,
            retention_days=body.retention_days,
            parallel=body.parallel,
            file_size_gb=body.file_size_gb,
            content=body.content,
            consistent_snapshot=body.consistent_snapshot,
            next_run_at=next_run,
        )
        with self.metadata_session.cursor() as cursor:
            cursor.setinputsizes(schema_names=oracledb.DB_TYPE_CLOB)
            parameters = {
                "id": task.id,
                "name": task.name,
                "target_id": task.target_id,
                "schema_name": task.schema_name,
                "directory": task.directory,
                "enabled": int(task.enabled),
                "cron": task.cron,
                "timezone": task.timezone,
                "retention_days": task.retention_days,
                "parallel": task.parallel,
                "file_size_gb": task.file_size_gb,
                "content": task.content,
                "consistent_snapshot": int(task.consistent_snapshot),
                "next_run_at": task.next_run_at,
                "export_mode": task.export_mode,
                "schema_names": task.schema_names,
            }
            cursor.execute(
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
                parameters,
            )

        self.metadata.record_audit(user=user, action="task_created", resource_id=task.id)
        self.metadata_session.commit()
        return task

    def update_task(self, task_id: str, body: TaskInput, user: User) -> Task:
        """Update future runs; existing runs retain their immutable settings snapshot.

        Args:
            task_id (str): Identifier of the owned export task.
            body (TaskInput): Validated workflow input.
            user (User): Authenticated actor for auditing.

        Returns:
            Task: The updated task.
        """
        if body.target_id != self.target_id:
            raise ApplicationError(status_code=409, detail="Task belongs to a different database")
        task = self._task(task_id=task_id, lock=True)
        self.validate_target(body=body)
        task.name = body.name
        task.target_id = body.target_id
        task.schema_name = body.schema_name or "FULL"
        task.schema_names = json.dumps(obj=body.schema_names)
        task.export_mode = body.export_mode
        task.directory = body.directory
        task.enabled = body.enabled
        task.cron = body.cron
        task.timezone = body.timezone
        task.retention_days = body.retention_days
        task.parallel = body.parallel
        task.file_size_gb = body.file_size_gb
        task.content = body.content
        task.consistent_snapshot = body.consistent_snapshot
        task.next_run_at = OracleDatabase.next_occurrence(expression=task.cron, zone=task.timezone, after=self.clock())
        with self.metadata_session.cursor() as cursor:
            parameters = {"next_run_at": task.next_run_at, "record_key": task.id}
            cursor.execute(("UPDATE tasks SET next_run_at = :next_run_at WHERE id = :record_key"), parameters)
        with self.metadata_session.cursor() as cursor:
            cursor.setinputsizes(schema_names=oracledb.DB_TYPE_CLOB)
            parameters = {
                "id": task.id,
                "name": task.name,
                "target_id": task.target_id,
                "schema_name": task.schema_name,
                "directory": task.directory,
                "enabled": int(task.enabled),
                "cron": task.cron,
                "timezone": task.timezone,
                "retention_days": task.retention_days,
                "parallel": task.parallel,
                "file_size_gb": task.file_size_gb,
                "content": task.content,
                "consistent_snapshot": int(task.consistent_snapshot),
                "next_run_at": task.next_run_at,
                "export_mode": task.export_mode,
                "schema_names": task.schema_names,
            }
            cursor.execute(
                (
                    "UPDATE tasks SET name = :name, target_id = :target_id, "
                    "schema_name = :schema_name, directory = :directory, "
                    "enabled = :enabled, cron = :cron, timezone = :timezone,"
                    " retention_days = :retention_days, parallel = "
                    ":parallel, file_size_gb = :file_size_gb, content = "
                    ":content, consistent_snapshot = :consistent_snapshot, "
                    "next_run_at = :next_run_at, export_mode = :export_mode,"
                    " schema_names = :schema_names WHERE id = :id"
                ),
                parameters,
            )
        self.metadata.record_audit(user=user, action="task_updated", resource_id=task.id)
        self.metadata_session.commit()
        return task

    def validate_target(self, body: TaskInput) -> None:
        """Verify actual target capability and privileges for the requested options.

        Args:
            body (TaskInput): Validated workflow input.
        """
        if body.target_id != self.target_id:
            raise ApplicationError(status_code=409, detail="Task belongs to a different database")
        target = self.target(lock=True)
        if not target.provisioned:
            raise ValueError("Provision the target before creating export tasks")
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": target.export_credential_id},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        export_cred = rows[0] if rows else None
        if export_cred is None:
            raise ValueError("Required credential record is missing")
        with self.connected(dsn=target.dsn, credential=export_cred):
            self.verify_export_task(body)

    def verify_export_task(self, task: TaskInput) -> None:
        """Verify account privileges and directory read/write before accepting a task.

        Args:
            task (TaskInput): Export policy task input with schemas, directory, parallel, etc.

        Raises:
            ValueError: If schema, role, parallel edition support, or flashback is missing.
        """
        # Ensure every requested export schema exists and is not Oracle internal.
        for name in task.schema_names:
            self.oracle_cursor.execute(
                ("SELECT COUNT(*) FROM all_users WHERE username = :name AND oracle_maintained='N'"),
                name=name,
            )
            if not self.oracle_cursor.fetchone()[0]:
                raise ValueError(f"Application schema {name} does not exist")
        # Ensure dedicated export account has DATAPUMP_EXP_FULL_DATABASE role.
        self.oracle_cursor.execute("SELECT role FROM session_roles")
        role_names = {row[0] for row in self.oracle_cursor}
        if "DATAPUMP_EXP_FULL_DATABASE" not in role_names:
            raise ValueError("Export account lacks DATAPUMP_EXP_FULL_DATABASE")
        # Oracle Standard Edition does not support parallel Data Pump workers.
        if task.parallel > 1:
            self.oracle_cursor.execute("SELECT banner_full FROM v$version WHERE ROWNUM = 1")
            version_banner = self.oracle_cursor.fetchone()[0]
            if "Enterprise Edition" not in version_banner:
                raise ValueError("Parallel exports require Enterprise Edition")
        # Point-in-time consistent exports require FLASHBACK ANY TABLE privilege and SCN access.
        if task.consistent_snapshot:
            self.oracle_cursor.execute("SELECT privilege FROM session_privs")
            priv_names = {row[0] for row in self.oracle_cursor}
            if "FLASHBACK ANY TABLE" not in priv_names:
                raise ValueError("Consistent export requires FLASHBACK ANY TABLE")
            self.oracle_cursor.execute(("SELECT DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER FROM dual"))
            self.oracle_cursor.fetchone()
        # Verify read/write OS filesystem permissions via temporary probe file.
        self.verify_directory(task.directory)

    @staticmethod
    def next_occurrence(expression: str, zone: str, after: datetime) -> datetime:
        """Find the next UTC instant, skipping DST gaps and repeated local times.

        Evaluates cron expressions against local wall-clock time in the configured IANA
        timezone to properly respect Daylight Saving Time transitions, then converts
        the result to a naive UTC datetime for persistence.

        Args:
            expression (str): Five-field cron expression (minute hour day month day-of-week).
            zone (str): IANA timezone name (e.g., 'America/Detroit' or 'UTC').
            after (datetime): UTC baseline timestamp, exclusive lower bound.

        Returns:
            datetime: Naive UTC timestamp representing the next scheduled occurrence.

        Raises:
            ValueError: If no valid occurrence exists within a five-year horizon.
        """
        # Load the IANA timezone and normalize the baseline to timezone-aware UTC.
        tz = ZoneInfo(key=zone)
        instant = after.replace(tzinfo=timezone.utc) if after.tzinfo is None else after

        # Project UTC instant to local wall-clock time (as naive datetime for croniter).
        local = instant.astimezone(tz=tz).replace(tzinfo=None)

        # Seed croniter one minute prior to allow matches occurring in the current minute.
        seed_time = local - timedelta(minutes=1)
        iterator = croniter(expr_format=expression, start_time=seed_time)

        # Search up to 3,000,000 iterations or a maximum of 5 calendar years into the future.
        for _ in range(3_000_000):
            candidate = iterator.get_next(ret_type=datetime)
            max_year = local.year + 5
            if candidate.year > max_year:
                break

            # Disambiguate fall-back overlap: fold=0 selects the earlier occurrence.
            aware = candidate.replace(tzinfo=tz, fold=0)
            utc = aware.astimezone(tz=timezone.utc)

            # Disambiguate spring-forward gap: if reconverting UTC to local wall-clock time
            # yields a different datetime, the candidate was a non-existent wall-clock instant.
            reconverted = utc.astimezone(tz=tz).replace(tzinfo=None)
            if reconverted != candidate:
                continue

            # Invariant: Occurrence must strictly follow the baseline instant.
            if utc > instant:
                naive_utc = utc.replace(tzinfo=None)
                return naive_utc

        raise ValueError("Schedule has no valid occurrence in the next five years")

    # Export lifecycle

    def queue_export(self, task_id: str, user: User) -> Run:
        """Queue a manual export under the same task lock used by the scheduler.

        Args:
            task_id (str): Identifier of the owned export task.
            user (User): Authenticated actor for auditing.

        Returns:
            Run: The committed queued export.
        """
        task = self._task(task_id=task_id, lock=True)
        try:
            run = self.enqueue(task=task)
        except ValueError as exc:
            raise ApplicationError(status_code=409, detail=str(exc)) from exc
        self.metadata.record_audit(user=user, action="run_queued", resource_id=run.id)
        self.metadata_session.commit()
        return run

    def cancel_export(self, run_id: str, user: User) -> Run:
        """Cancel queued work immediately or request a monitored Oracle stop.

        Args:
            run_id (str): Identifier of the owned export.
            user (User): Authenticated actor for auditing.

        Returns:
            Run: The updated export.
        """
        run = self.run(run_id=run_id, lock=True)
        if run.state in TERMINAL:
            raise ApplicationError(status_code=409, detail="Run is already terminal")
        execution = MetadataService.execution(run=run)
        execution.request_cancel(requested_at=self.clock())
        self.metadata.save_execution(run=run, execution=execution)
        self.metadata.record_audit(user=user, action="run_cancel_requested", resource_id=run.id)
        self.metadata_session.commit()
        return run

    def reconcile_export(self, run_id: str, body: ReconcileInput, user: User) -> Run:
        """Resolve stranded inactive jobs explicitly without asserting an unproven success.

        Args:
            run_id (str): Identifier of the owned export.
            body (ReconcileInput): Validated workflow input.
            user (User): Authenticated actor for auditing.

        Returns:
            Run: The resolved export.
        """
        run = self.run(run_id=run_id, lock=True)
        if run.state != "reconciliation_required":
            raise ApplicationError(status_code=409, detail="Only stranded runs can be acknowledged")
        options = RunSnapshot.from_json(value=run.snapshot).options()
        target_dsn = options["dsn"]
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": options["credential_id"]},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        cred = rows[0] if rows else None
        if cred is None:
            raise ValueError("Required credential record is missing")
        with self.connected(dsn=target_dsn, credential=cred):
            self.verify_datapump_inactive(run.oracle_job)
        execution = MetadataService.execution(run=run)
        execution.acknowledge(outcome=body.outcome, reason=body.reason, acknowledged_at=self.clock())
        self.metadata.save_execution(run=run, execution=execution)
        if body.outcome == "failed":
            outbox_payload = {"run_id": run.id, "task": options["name"], "reason": body.reason}
            outbox = NotificationService()
            outbox.queue_event(db=self.metadata_session, event="export_failed", details=EventDetails(root=outbox_payload))
        run.notification_created = True
        with self.metadata_session.cursor() as cursor:
            parameters = {
                "notification_created": int(run.notification_created),
                "record_key": run.id,
            }
            cursor.execute(
                ("UPDATE runs SET notification_created = :notification_created WHERE id = :record_key"),
                parameters,
            )
        audit_action = "run_reconciled_" + body.outcome
        self.metadata.record_audit(user=user, action=audit_action, resource_id=run.id)
        self.metadata_session.commit()
        return run

    def enqueue(self, task: Task, occurrence: str | None = None) -> Run:
        """Queue one immutable export; callers hold the task row lock until commit.

        Args:
            task (Task): Export task belonging to this database.
            occurrence (str | None): Scheduled occurrence tag or None for a manual export.

        Returns:
            Run: The queued export; the caller commits.
        """
        self._require_owner(item=task)
        if self.metadata.has_busy_task(task_id=task.id):
            raise ValueError("This task already has a pending or active export")
        with self.metadata_session.cursor() as cursor:
            cursor.execute("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key", {"record_key": task.target_id})
            rows = [Target(id=row[0], registration_id=row[1], name=row[2], dsn=row[3], admin_credential_id=row[4], export_credential_id=row[5], directory=row[6], tablespace=row[7], provisioned=row[8], edition=row[9], container_name=row[10]) for row in cursor.fetchall()]
        target = rows[0] if rows else None
        if not target or not target.provisioned:
            raise ValueError("Target must be provisioned before exporting")
        run_id = self.id_factory()
        created_at = self.clock()
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key",
                {"record_key": target.registration_id},
            )
            rows = [Registration(id=row[0], name=row[1], dsn=row[2], credential_id=row[3], kind=row[4], version=row[5]) for row in cursor.fetchall()]
        registration = rows[0] if rows else None
        if registration is None:
            raise ValueError("Required registration record is missing")
        schemas_list = json.loads(s=task.schema_names) or ([task.schema_name] if task.export_mode == "SCHEMA" else [])
        database = registration.name
        if registration.kind != "non_cdb" and target.container_name != "CDB$ROOT":
            pdb = target.container_name
        else:
            pdb = None
        for id_length in range(6, 33, 2):
            prefix = OracleDatabase._readable_prefix(created_at=created_at, database=database, pdb=pdb, schemas=schemas_list, export_mode=task.export_mode, run_id=run_id, id_length=id_length)
            if not self.metadata.filename_exists(filename=prefix + ".log"):
                break
        else:
            raise ValueError("Unable to allocate unique export filenames")
        if target.export_credential_id is None:
            raise ValueError("Target must have export credentials before exporting")
        snapshot = QueuedRunSnapshot.model_validate(
            obj={
                "name": task.name,
                "schema_name": task.schema_name,
                "directory": task.directory,
                "parallel": task.parallel,
                "file_size_gb": task.file_size_gb,
                "content": task.content,
                "consistent_snapshot": task.consistent_snapshot,
                "retention_days": task.retention_days,
                "cron": task.cron,
                "timezone": task.timezone,
                "export_mode": task.export_mode,
                "schema_names": schemas_list,
                "target_name": target.name,
                "dsn": target.dsn,
                "credential_id": target.export_credential_id,
                "database_name": database,
                "pdb_name": pdb,
                "file_prefix": prefix,
            }
        )
        run = Run(id=run_id, created_at=created_at, task_id=task.id, target_id=task.target_id, occurrence=occurrence or f"manual:{run_id}", snapshot=snapshot.model_dump_json(), oracle_job=f"OE_{run_id[:27].upper()}", state="queued")
        with self.metadata_session.cursor() as cursor:
            cursor.setinputsizes(
                snapshot=oracledb.DB_TYPE_CLOB,
                errors=oracledb.DB_TYPE_CLOB,
                messages=oracledb.DB_TYPE_CLOB,
            )
            parameters = {
                "id": run.id,
                "task_id": run.task_id,
                "target_id": run.target_id,
                "occurrence": run.occurrence,
                "snapshot": run.snapshot,
                "state": run.state,
                "oracle_job": run.oracle_job,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "progress": run.progress,
                "errors": run.errors,
                "messages": run.messages,
                "cancel_requested": int(run.cancel_requested),
                "last_seen_at": run.last_seen_at,
                "notification_created": int(run.notification_created),
                "master_cleanup_pending": int(run.master_cleanup_pending),
            }
            cursor.execute(
                (
                    "INSERT INTO runs (id, task_id, target_id, occurrence, "
                    "snapshot, state, oracle_job, created_at, started_at, "
                    "finished_at, progress, errors, messages, "
                    "cancel_requested, last_seen_at, notification_created, master_cleanup_pending) "
                    "VALUES (:id, :task_id, :target_id, :occurrence, "
                    ":snapshot, :state, :oracle_job, :created_at, "
                    ":started_at, :finished_at, :progress, :errors, "
                    ":messages, :cancel_requested, :last_seen_at, "
                    ":notification_created, :master_cleanup_pending)"
                ),
                parameters,
            )

        log_name = prefix + ".log"
        log_file = BackupFile(run_id=run.id, directory=task.directory, filename=log_name, kind="log")
        with self.metadata_session.cursor() as cursor:
            cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
            parameters = {
                "id": log_file.id,
                "run_id": log_file.run_id,
                "directory": log_file.directory,
                "filename": log_file.filename,
                "kind": log_file.kind,
                "size_bytes": log_file.size_bytes,
                "deleted_at": log_file.deleted_at,
                "cleanup_attempts": log_file.cleanup_attempts,
                "cleanup_error": log_file.cleanup_error,
            }
            cursor.execute(
                ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
                parameters,
            )
        return run

    def start_export(self, run_id: str) -> None:
        """Attempt startup once; uncertain remote outcomes always require reconciliation.

        Args:
            run_id (str): Identifier of the owned export.
        """
        run = self.metadata.get_for_update(run_id=run_id)
        if run is not None:
            self._require_owner(item=run)
        if run is None or run.state != "starting":
            self.metadata_session.rollback()
            return
        options = RunSnapshot.from_json(value=run.snapshot).options()
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": options["credential_id"]},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        credential = rows[0] if rows else None
        if run.cancel_requested:
            run.state = "cancelled"
            run.finished_at = self.clock()
            with self.metadata_session.cursor() as cursor:
                parameters = {
                    "state": run.state,
                    "finished_at": run.finished_at,
                    "record_key": run.id,
                }
                cursor.execute(
                    ("UPDATE runs SET state = :state, finished_at = :finished_at WHERE id = :record_key"),
                    parameters,
                )
            self.metadata_session.commit()
            return
        try:
            if credential is None:
                raise ValueError("Required credential record is missing")
            with self.connected(dsn=options["dsn"], credential=credential):
                self.start_datapump_job(run.oracle_job, run.id, options)
            run.state = "running"
            with self.metadata_session.cursor() as cursor:
                parameters = {"state": run.state, "record_key": run.id}
                cursor.execute("UPDATE runs SET state = :state WHERE id = :record_key", parameters)
        except Exception as exc:
            run.state = "reconciliation_required"
            with self.metadata_session.cursor() as cursor:
                parameters = {"state": run.state, "record_key": run.id}
                cursor.execute("UPDATE runs SET state = :state WHERE id = :record_key", parameters)
            err_msg = "Startup outcome uncertain: " + safe_error(exc=exc)
            run.messages = json.dumps(obj=[err_msg])
            with self.metadata_session.cursor() as cursor:
                cursor.setinputsizes(messages=oracledb.DB_TYPE_CLOB)
                parameters = {"messages": run.messages, "record_key": run.id}
                cursor.execute("UPDATE runs SET messages = :messages WHERE id = :record_key", parameters)
        self.metadata_session.commit()

    def monitor_export(self, run_id: str) -> bool:
        """Reattach active jobs, reconcile log completion, and collect measured sizes.

        Args:
            run_id (str): Identifier of the owned export.

        Returns:
            bool: True if the export is terminal.
        """
        run = self.metadata.get_for_update(run_id=run_id)
        if run is not None:
            self._require_owner(item=run)
        if run is None or run.state not in ACTIVE:
            self.metadata_session.rollback()
            return False
        options = RunSnapshot.from_json(value=run.snapshot).options()
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": options["credential_id"]},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        credential = rows[0] if rows else None
        try:
            if credential is None:
                raise ValueError("Required credential record is missing")
            with self.connected(dsn=options["dsn"], credential=credential):
                status = None
                try:
                    status = self.datapump_job_status(run.oracle_job)
                    if run.cancel_requested and status.state not in {"COMPLETED", "STOPPED"}:
                        self.stop_datapump_job(run.oracle_job)
                        status = self.datapump_job_status(run.oracle_job)
                except Exception:
                    log_name = f"{OracleDatabase.file_prefix(run_id=run.id, options=options)}.log"
                    lines = self.read_log(options["directory"], log_name)
                    status = self.recover_from_log(job=run.oracle_job, run_id=run.id, lines=lines, options=options)
                if status is None:
                    run.state = "reconciliation_required"
                    with self.metadata_session.cursor() as cursor:
                        parameters = {"state": run.state, "record_key": run.id}
                        cursor.execute("UPDATE runs SET state = :state WHERE id = :record_key", parameters)
                else:
                    self.update_status(run=run, status=status)
                    if run.state in TERMINAL:
                        try:
                            log_name = f"{OracleDatabase.file_prefix(run_id=run.id, options=options)}.log"
                            lines = self.read_log(options["directory"], log_name)
                            recovered = self.recover_from_log(job=run.oracle_job, run_id=run.id, lines=lines, options=options)
                            if recovered:
                                self.update_status(run=run, status=recovered)
                        except Exception:
                            pass

                        for file in self.metadata.files(run_id=run.id):
                            try:
                                file_dir = file.directory
                                file_name = file.filename
                                file.size_bytes = self.file_size(file_dir, file_name)
                                with self.metadata_session.cursor() as cursor:
                                    parameters = {
                                        "size_bytes": file.size_bytes,
                                        "record_key": file.id,
                                    }
                                    cursor.execute(
                                        ("UPDATE backup_files SET size_bytes = :size_bytes WHERE id = :record_key"),
                                        parameters,
                                    )
                            except Exception:
                                file.size_bytes = None
                                with self.metadata_session.cursor() as cursor:
                                    parameters = {
                                        "size_bytes": file.size_bytes,
                                        "record_key": file.id,
                                    }
                                    cursor.execute(
                                        ("UPDATE backup_files SET size_bytes = :size_bytes WHERE id = :record_key"),
                                        parameters,
                                    )
        except Exception as exc:
            run.state = "reconciliation_required"
            with self.metadata_session.cursor() as cursor:
                parameters = {"state": run.state, "record_key": run.id}
                cursor.execute("UPDATE runs SET state = :state WHERE id = :record_key", parameters)
            messages = json.loads(s=run.messages)
            message = "Connection/reconciliation error: " + safe_error(exc=exc)
            combined_msgs = list(dict.fromkeys([*messages, message]))[-500:]
            run.messages = json.dumps(obj=combined_msgs)
            with self.metadata_session.cursor() as cursor:
                cursor.setinputsizes(messages=oracledb.DB_TYPE_CLOB)
                parameters = {"messages": run.messages, "record_key": run.id}
                cursor.execute("UPDATE runs SET messages = :messages WHERE id = :record_key", parameters)
        if run.state in TERMINAL and (not run.notification_created):
            if run.state in {"failed", "completed_with_errors"}:
                event_type = "export_" + run.state
                event_payload = {"run_id": run.id, "task": options["name"]}
                event_payload["target"] = options["target_name"]
                event_payload["errors"] = json.loads(s=run.errors)
                outbox = NotificationService()
                outbox.queue_event(db=self.metadata_session, event=event_type, details=EventDetails(root=event_payload))
            run.notification_created = True
            with self.metadata_session.cursor() as cursor:
                parameters = {
                    "notification_created": int(run.notification_created),
                    "record_key": run.id,
                }
                cursor.execute(
                    ("UPDATE runs SET notification_created = :notification_created WHERE id = :record_key"),
                    parameters,
                )
        completed = run.state in TERMINAL
        # Commit terminal history and outbox records before dropping the remote master table.
        self.metadata_session.commit()
        if completed:
            self.cleanup_master(run_id=run.id)
        return completed

    def update_status(self, run: Run, status: JobStatus) -> None:
        """Persist observed status and only owned exact file names.

        Args:
            run (Run): Export record belonging to this database.
            status (JobStatus): Observed Oracle Data Pump state and files.
        """
        self._require_owner(item=run)
        execution = MetadataService.execution(run=run)
        execution.apply_status(status=status, observed_at=self.clock())
        self.metadata.save_execution(run=run, execution=execution)
        options = json.loads(s=run.snapshot)
        directory = options["directory"]
        known = {f.filename for f in self.metadata.files(run_id=run.id)}
        for path in status.files:
            name = self.owned_filename(run_id=run.id, path=path, options=options)
            if name and name not in known:
                file_kind = "log" if name.endswith(".log") else "dump"
                with self.metadata_session.cursor() as cursor:
                    cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
                    backup_file = BackupFile(run_id=run.id, directory=directory, filename=name, kind=file_kind)
                    parameters = {
                        "id": backup_file.id,
                        "run_id": backup_file.run_id,
                        "directory": backup_file.directory,
                        "filename": backup_file.filename,
                        "kind": backup_file.kind,
                        "size_bytes": backup_file.size_bytes,
                        "deleted_at": backup_file.deleted_at,
                        "cleanup_attempts": backup_file.cleanup_attempts,
                        "cleanup_error": backup_file.cleanup_error,
                    }
                    cursor.execute(
                        ("INSERT INTO backup_files (id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error) VALUES (:id, :run_id, :directory, :filename, :kind, :size_bytes, :deleted_at, :cleanup_attempts, :cleanup_error)"),
                        parameters,
                    )
                known.add(name)

    # Retention and cleanup

    def expired_runs(self, task: Task) -> list[Run]:
        """Select terminal expired exports, preserving the latest successful run.

        Args:
            task (Task): Export task belonging to this database.

        Returns:
            list[Run]: Expired terminal exports, excluding the latest success.
        """
        self._require_owner(item=task)
        cutoff = self.clock() - timedelta(days=task.retention_days)
        return self.metadata.expired_runs(task=task, cutoff=cutoff)

    def delete_export_files(self, run_id: str, user: User) -> Run:
        """Delete a terminal export's exact owned files while preserving execution history.

        Args:
            run_id (str): Identifier of the owned export.
            user (User): Authenticated actor for auditing.

        Returns:
            Run: The export with updated file deletion records.
        """
        run = self.run(run_id=run_id, lock=True)
        if run.state not in TERMINAL:
            raise ApplicationError(status_code=409, detail="Finish or cancel the export before deleting its files")
        options = json.loads(s=run.snapshot)
        files = self.metadata.files(run_id=run.id, retained=True, lock=True)
        if not files:
            return run
        try:
            for file in files:
                self._validate_file(run=run, file=file)
        except ValueError as exc:
            raise ApplicationError(status_code=409, detail="File manifest is outside the export namespace") from exc
        failed = False
        target_dsn = options["dsn"]
        with self.metadata_session.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": options["credential_id"]},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        cred_record = rows[0] if rows else None
        try:
            if cred_record is None:
                raise ValueError("Required credential record is missing")
            with self.connected(dsn=target_dsn, credential=cred_record):
                self.verify_datapump_inactive(run.oracle_job)
                for file in files:
                    file.cleanup_attempts += 1
                    with self.metadata_session.cursor() as cursor:
                        parameters = {
                            "cleanup_attempts": file.cleanup_attempts,
                            "record_key": file.id,
                        }
                        cursor.execute(
                            ("UPDATE backup_files SET cleanup_attempts = :cleanup_attempts WHERE id = :record_key"),
                            parameters,
                        )
                    try:
                        self._delete_file(file=file)
                    except Exception as exc:
                        file.cleanup_error = safe_error(exc=exc)
                        with self.metadata_session.cursor() as cursor:
                            cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
                            parameters = {
                                "cleanup_error": file.cleanup_error,
                                "record_key": file.id,
                            }
                            cursor.execute(
                                ("UPDATE backup_files SET cleanup_error = :cleanup_error WHERE id = :record_key"),
                                parameters,
                            )
                        failed = True
        except Exception as exc:
            for file in files:
                if file.deleted_at is None:
                    file.cleanup_attempts += 1
                    file.cleanup_error = safe_error(exc=exc)
                    with self.metadata_session.cursor() as cursor:
                        cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
                        parameters = {
                            "cleanup_attempts": file.cleanup_attempts,
                            "cleanup_error": file.cleanup_error,
                            "record_key": file.id,
                        }
                        cursor.execute(
                            ("UPDATE backup_files SET cleanup_attempts = :cleanup_attempts, cleanup_error = :cleanup_error WHERE id = :record_key"),
                            parameters,
                        )
            failed = True
        audit_action = "export_files_delete_failed" if failed else "export_files_deleted"
        self.metadata.record_audit(user=user, action=audit_action, resource_id=run.id)
        self.metadata_session.commit()
        if failed:
            raise ApplicationError(status_code=502, detail="Some export files could not be deleted. Review file errors and retry.")
        return run

    def cleanup_master(self, run_id: str) -> None:
        """Retry remote master removal under a run lock, independently of retained files.

        Args:
            run_id (str): Owned terminal run whose cleanup intent was committed.
        """
        run = self.run(run_id=run_id, lock=True)
        if run.state not in TERMINAL or not run.master_cleanup_pending:
            self.metadata_session.rollback()
            return
        options = RunSnapshot.from_json(value=run.snapshot).options()
        try:
            with self.metadata_session.cursor() as cursor:
                cursor.execute("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :id", {"id": options["credential_id"]})
                row = cursor.fetchone()
                credential = Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) if row else None
            if credential is None:
                raise ValueError("Required credential record is missing")
            with self.connected(dsn=options["dsn"], credential=credential):
                self.release_datapump_master(run.oracle_job)
            with self.metadata_session.cursor() as cursor:
                cursor.execute("UPDATE runs SET master_cleanup_pending = 0 WHERE id = :id", {"id": run.id})
        except Exception as exc:
            message = "Master-table cleanup deferred: " + safe_error(exc=exc)
            messages = list(dict.fromkeys([*json.loads(s=run.messages), message]))[-500:]
            with self.metadata_session.cursor() as cursor:
                cursor.setinputsizes(messages=oracledb.DB_TYPE_CLOB)
                cursor.execute(
                    "UPDATE runs SET messages = :messages WHERE id = :id",
                    {"messages": json.dumps(obj=messages), "id": run.id},
                )
        self.metadata_session.commit()

    def cleanup(self, renew_lease: Callable[[Connection], bool]) -> None:
        """Apply retention only to terminal owned files, preserving the latest success.

        Args:
            renew_lease (Callable[[Connection], bool]): Worker leadership renewal callback.
        """
        if self.target_id is None:
            raise ValueError("Cleanup requires a target")
        for run_id in self.metadata.pending_master_cleanup(target_id=self.target_id):
            if not renew_lease(self.metadata_session):
                return
            self.cleanup_master(run_id=run_id)
        task_ids = self.metadata.task_ids(target_id=self.target_id)
        for task_id in task_ids:
            lease_active = renew_lease(self.metadata_session)
            if not lease_active:
                return
            with self.metadata_session.cursor() as cursor:
                cursor.execute("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE id = :record_key", {"record_key": task_id})
                rows = [
                    Task(
                        id=row[0],
                        name=row[1],
                        target_id=row[2],
                        schema_name=row[3],
                        directory=row[4],
                        enabled=row[5],
                        cron=row[6],
                        timezone=row[7],
                        retention_days=row[8],
                        parallel=row[9],
                        file_size_gb=row[10],
                        content=row[11],
                        consistent_snapshot=row[12],
                        next_run_at=row[13],
                        export_mode=row[14],
                        schema_names=row[15].read() if hasattr(row[15], "read") else row[15],
                    )
                    for row in cursor.fetchall()
                ]
            task = rows[0] if rows else None
            if task is None:
                raise ValueError("Required task record is missing")
            for run in self.expired_runs(task=task):
                options = json.loads(s=run.snapshot)
                files = self.metadata.files(run_id=run.id, retained=True)
                if not files:
                    continue
                target_dsn = options["dsn"]
                with self.metadata_session.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                        {"record_key": options["credential_id"]},
                    )
                    rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
                cred_record = rows[0] if rows else None
                try:
                    if cred_record is None:
                        raise ValueError("Required credential record is missing")
                    with self.connected(dsn=target_dsn, credential=cred_record):
                        for file_item in files:
                            file = self.metadata.file_for_update(file_id=file_item.id)
                            if file is None or file.deleted_at is not None:
                                continue
                            file.cleanup_attempts += 1
                            with self.metadata_session.cursor() as cursor:
                                parameters = {
                                    "cleanup_attempts": file.cleanup_attempts,
                                    "record_key": file.id,
                                }
                                cursor.execute(
                                    ("UPDATE backup_files SET cleanup_attempts = :cleanup_attempts WHERE id = :record_key"),
                                    parameters,
                                )
                            try:
                                self._validate_file(run=run, file=file)
                                self._delete_file(file=file)
                            except Exception as exc:
                                file.cleanup_error = safe_error(exc=exc)
                                with self.metadata_session.cursor() as cursor:
                                    cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
                                    parameters = {
                                        "cleanup_error": file.cleanup_error,
                                        "record_key": file.id,
                                    }
                                    cursor.execute(
                                        ("UPDATE backup_files SET cleanup_error = :cleanup_error WHERE id = :record_key"),
                                        parameters,
                                    )
                                if file.cleanup_attempts == 1:
                                    outbox = NotificationService()
                                    event_details = {"run_id": run.id}
                                    event_details["filename"] = file.filename
                                    event_details["error"] = file.cleanup_error
                                    outbox.queue_event(db=self.metadata_session, event="cleanup_failed", details=EventDetails(root=event_details))
                            self.metadata_session.commit()

                except Exception as exc:
                    for file_rec in files:
                        file_rec.cleanup_attempts += 1
                        file_rec.cleanup_error = safe_error(exc=exc)
                        with self.metadata_session.cursor() as cursor:
                            cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
                            parameters = {
                                "cleanup_attempts": file_rec.cleanup_attempts,
                                "cleanup_error": file_rec.cleanup_error,
                                "record_key": file_rec.id,
                            }
                            cursor.execute(
                                ("UPDATE backup_files SET cleanup_attempts = :cleanup_attempts, cleanup_error = :cleanup_error WHERE id = :record_key"),
                                parameters,
                            )
                        if file_rec.cleanup_attempts == 1:
                            outbox = NotificationService()
                            event_details = {"run_id": run.id}
                            event_details["filename"] = file_rec.filename
                            event_details["error"] = file_rec.cleanup_error
                            outbox.queue_event(db=self.metadata_session, event="cleanup_failed", details=EventDetails(root=event_details))
                    self.metadata_session.commit()

    def _delete_file(self, file: BackupFile) -> None:
        """Remove one exact file and record success after the remote call returns.

        Args:
            file (BackupFile): Recorded export file.
        """
        self.remove_file(file.directory, file.filename)
        file.deleted_at = self.clock()
        file.cleanup_error = None
        with self.metadata_session.cursor() as cursor:
            cursor.setinputsizes(cleanup_error=oracledb.DB_TYPE_CLOB)
            parameters = {
                "deleted_at": file.deleted_at,
                "cleanup_error": file.cleanup_error,
                "record_key": file.id,
            }
            cursor.execute(
                ("UPDATE backup_files SET deleted_at = :deleted_at, cleanup_error = :cleanup_error WHERE id = :record_key"),
                parameters,
            )

    def _validate_file(self, run: Run, file: BackupFile) -> None:
        """Reject files outside the owning export's captured directory and namespace.

        Args:
            run (Run): Export record belonging to this database.
            file (BackupFile): Recorded export file.
        """
        self._require_owner(item=run)
        options = json.loads(s=run.snapshot)
        if file.run_id != run.id or file.directory != options["directory"] or self.owned_filename(run_id=run.id, path=file.filename, options=options) != file.filename:
            raise ValueError("Filename is outside the run namespace")

    # Data Pump operations and recovery

    def start_datapump_job(self, job: str, run_id: str, options: dict) -> None:
        """Start a schema or full export, retaining its master table for recovery.

        Configures DBMS_DATAPUMP with %L parallel dump file templating, metadata compression,
        optional flashback SCN point-in-time consistency, and KEEP_MASTER retention.

        Args:
            job (str): Oracle Data Pump unique job name (prefixed with OE_).
            run_id (str): Application export run identifier for file naming.
            options (dict): Validated export configuration dictionary.

        Raises:
            ValueError: If export_mode is invalid or schema list is empty for SCHEMA mode.
        """
        mode = options.get("export_mode", "SCHEMA")
        if mode not in {"SCHEMA", "FULL"}:
            raise ValueError("Invalid export mode")
        # Fall back to single schema_name if schema_names array is not provided.
        schema_name = options.get("schema_name")
        fallback_schemas = [schema_name] if mode == "SCHEMA" and isinstance(schema_name, str) else []
        schemas = options.get("schema_names") or fallback_schemas
        if mode == "SCHEMA" and not schemas:
            raise ValueError("Select at least one schema")
        # Build SCHEMA_EXPR filter string: IN ('SCHEMA1', 'SCHEMA2').
        quoted_names = ", ".join(f"'{identifier(value=name)}'" for name in schemas)
        schema_expr = f"IN ({quoted_names})" if schemas else None
        # Construct dump file template using %L for 2-digit parallel worker file numbering.
        dump_template = f"{OracleDatabase.file_prefix(run_id=run_id, options=options)}_%L.dmp"
        log_filename = f"{OracleDatabase.file_prefix(run_id=run_id, options=options)}.log"
        target_dir = identifier(value=options["directory"])
        chunk_size = f"{int(options['file_size_gb'])}G"
        parallel_degree = int(options["parallel"])
        content_filter = options["content"]
        use_consistent = int(options["consistent_snapshot"])
        self.oracle_cursor.execute(
            """
            DECLARE h NUMBER;
            BEGIN
              -- Open Data Pump export handle in SCHEMA or FULL export mode.
              h := DBMS_DATAPUMP.OPEN('EXPORT', :export_mode, job_name => :job);
              IF :export_mode = 'SCHEMA' THEN
                DBMS_DATAPUMP.METADATA_FILTER(h, 'SCHEMA_EXPR', :schema_expr);
              END IF;
              -- Add dump file using %L format specifier to generate unique worker filenames.
              DBMS_DATAPUMP.ADD_FILE(h, :dump, :directory, :filesize,
                  DBMS_DATAPUMP.KU$_FILE_TYPE_DUMP_FILE, reusefile => 0);
              DBMS_DATAPUMP.ADD_FILE(h, :logfile, :directory,
                  filetype => DBMS_DATAPUMP.KU$_FILE_TYPE_LOG_FILE, reusefile => 0);
              -- Set parallelism degree for multi-process worker throughput.
              DBMS_DATAPUMP.SET_PARALLEL(h, :parallel);
              DBMS_DATAPUMP.SET_PARAMETER(h, 'COMPRESSION', 'METADATA_ONLY');
              -- KEEP_MASTER ensures the master table is retained for state reconciliation.
              DBMS_DATAPUMP.SET_PARAMETER(h, 'KEEP_MASTER', 1);
              IF :content = 'DATA_ONLY' THEN
                DBMS_DATAPUMP.SET_PARAMETER(h, 'INCLUDE_METADATA', 0);
              ELSIF :content = 'METADATA_ONLY' THEN
                DBMS_DATAPUMP.DATA_FILTER(h, 'INCLUDE_ROWS', 0);
              END IF;
              IF :consistent = 1 THEN
                -- Pin export to current SCN for consistent point-in-time snapshot.
                DBMS_DATAPUMP.SET_PARAMETER(h, 'FLASHBACK_SCN',
                    DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER);
              END IF;
              -- Start background job without RAC cluster distribution (single node export).
              DBMS_DATAPUMP.START_JOB(h, cluster_ok => 0);
              -- Detach client session so Data Pump runs independently on the database server.
              DBMS_DATAPUMP.DETACH(h);
            EXCEPTION WHEN OTHERS THEN
              IF h IS NOT NULL THEN
                BEGIN DBMS_DATAPUMP.DETACH(h); EXCEPTION WHEN OTHERS THEN NULL; END;
              END IF;
              RAISE;
            END;
        """,
            job=job,
            export_mode=mode,
            schema_expr=schema_expr,
            dump=dump_template,
            logfile=log_filename,
            directory=target_dir,
            filesize=chunk_size,
            parallel=parallel_degree,
            content=content_filter,
            consistent=use_consistent,
        )

    def datapump_job_status(self, job: str) -> JobStatus:
        """Attach and extract status using PL/SQL to avoid version-specific Python types.

        Attaches to the active or stopped Data Pump job using DBMS_DATAPUMP.ATTACH and
        queries status mask 15 to retrieve percent done, error count, and log buffers.

        Args:
            job (str): Oracle Data Pump job name to monitor.

        Returns:
            JobStatus: Extracted job status with state, percent done, error count, and logs.
        """
        # Allocate output cursor variables for scalar and CLOB buffers.
        state = self.oracle_cursor.var(str, size=100)
        progress = self.oracle_cursor.var(oracledb.NUMBER)
        count = self.oracle_cursor.var(oracledb.NUMBER)
        messages = self.oracle_cursor.var(oracledb.DB_TYPE_CLOB)
        errors = self.oracle_cursor.var(oracledb.DB_TYPE_CLOB)
        files = self.oracle_cursor.var(oracledb.DB_TYPE_CLOB)
        self.oracle_cursor.execute(
            """
            DECLARE h NUMBER; s ku$_Status; st VARCHAR2(100); i BINARY_INTEGER;
                    m CLOB := ''; e CLOB := ''; f CLOB := '';
            BEGIN
              -- Reattach to existing job; timeout 0 returns immediately without blocking.
              h := DBMS_DATAPUMP.ATTACH(:job);
              -- Mask 15 = JOB_DESC + JOB_STATUS + WIP + ERROR status masks.
              DBMS_DATAPUMP.GET_STATUS(h, 15, 0, st, s);
              :state := st;
              -- Mask bit 4 indicates job_status structure is present.
              IF BITAND(s.mask, 4) != 0 THEN
                :progress := s.job_status.percent_done;
                :error_count := s.job_status.error_count;
                BEGIN
                  i := s.job_status.files.FIRST;
                  WHILE i IS NOT NULL LOOP
                    IF s.job_status.files(i).file_type = 0 THEN
                      f := f || s.job_status.files(i).file_name || CHR(10);
                    END IF;
                    i := s.job_status.files.NEXT(i);
                  END LOOP;
                EXCEPTION WHEN COLLECTION_IS_NULL THEN NULL;
                END;
              END IF;
              -- Mask bit 1 indicates work-in-progress log messages are present.
              IF BITAND(s.mask, 1) != 0 THEN
                i := s.wip.FIRST;
                WHILE i IS NOT NULL LOOP
                  m := m || s.wip(i).LogText || CHR(10); i := s.wip.NEXT(i);
                END LOOP;
              END IF;
              -- Mask bit 8 indicates error message entries are present.
              IF BITAND(s.mask, 8) != 0 THEN
                i := s.error.FIRST;
                WHILE i IS NOT NULL LOOP
                  e := e || s.error(i).LogText || CHR(10); i := s.error.NEXT(i);
                END LOOP;
              END IF;
              :messages := m; :errors := e; :files := f;
              DBMS_DATAPUMP.DETACH(h);
            EXCEPTION WHEN OTHERS THEN
              IF h IS NOT NULL THEN
                BEGIN DBMS_DATAPUMP.DETACH(h); EXCEPTION WHEN OTHERS THEN NULL; END;
              END IF;
              RAISE;
            END;
        """,
            job=job,
            state=state,
            progress=progress,
            error_count=count,
            messages=messages,
            errors=errors,
            files=files,
        )
        # Parse scalar and CLOB returns into immutable JobStatus domain representation.
        st_val = state.getvalue()
        raw_progress = progress.getvalue()
        prog_int = int(raw_progress) if raw_progress is not None else None
        err_int = int(count.getvalue() or 0)
        err_lines = OracleUtilities.lob_lines(variable=errors)
        msg_lines = OracleUtilities.lob_lines(variable=messages)
        file_lines = OracleUtilities.lob_lines(variable=files)
        return JobStatus(
            state=st_val,
            progress=prog_int,
            error_count=err_int,
            errors=err_lines,
            messages=msg_lines,
            files=file_lines,
        )

    def stop_datapump_job(self, job: str) -> None:
        """Stop a job and retain its master table for explicit reconciliation.

        Attaches to the job and calls DBMS_DATAPUMP.STOP_JOB with immediate => 1 and
        keep_master => 1 so the master table is preserved for error inspection.

        Args:
            job (str): Oracle Data Pump job name to cancel.
        """
        # Attach and instruct Data Pump to immediately abort with master retention.
        self.oracle_cursor.execute(
            """
            DECLARE h NUMBER;
            BEGIN
              h := DBMS_DATAPUMP.ATTACH(:job);
              DBMS_DATAPUMP.STOP_JOB(h, immediate => 1, keep_master => 1);
            END;
        """,
            job=job,
        )

    def verify_datapump_inactive(self, job: str) -> None:
        """Require a missing or stopped Oracle job before explicit failure acknowledgment.

        Inspects user_datapump_jobs and dba_datapump_sessions to confirm no active
        workers or attached client sessions exist.

        Args:
            job (str): Oracle Data Pump job name to verify.

        Raises:
            ValueError: If the job is still actively running or attached sessions remain.
        """
        # Check current job state from Oracle data dictionary view.
        self.oracle_cursor.execute(("SELECT state FROM user_datapump_jobs WHERE job_name = :job"), job=job)
        row = self.oracle_cursor.fetchone()
        if row and row[0] not in {"NOT RUNNING", "STOPPED", "DEFINING"}:
            raise ValueError("Oracle still reports an active job; cancel it before acknowledgment")
        if row:
            # A defining job could still have a live client preparing it. Never race that client.
            self.oracle_cursor.execute(
                ("SELECT COUNT(*) FROM dba_datapump_sessions WHERE job_name = :job AND owner_name = USER"),
                job=job,
            )
            attached_count = self.oracle_cursor.fetchone()[0]
            if attached_count:
                raise ValueError("Oracle job still has attached sessions")

    def release_datapump_master(self, job: str) -> None:
        """Remove a confirmed inactive application-owned master table after history commits.

        Verifies table name against strict application regex pattern and confirms inactivity
        before issuing DROP TABLE PURGE.

        Args:
            job (str): Master table name to purge from the database.

        Raises:
            ValueError: If table name is outside the application namespace or job is still active.
        """
        # Enforce strict OE_<hex> identifier pattern to eliminate SQL injection vectors.
        if not re.fullmatch(pattern=r"OE_[A-F0-9]{27}", string=job):
            raise ValueError("Master table name is outside the application namespace")
        # Ensure job is completely inactive before purging master table.
        self.verify_datapump_inactive(job)
        self.oracle_cursor.execute(("SELECT COUNT(*) FROM user_tables WHERE table_name = :job"), job=job)
        table_exists = self.oracle_cursor.fetchone()[0]
        if table_exists:
            # Purge table immediately without sending it to the Oracle RecycleBin.
            self.oracle_cursor.execute(f"DROP TABLE {job} PURGE")

    @staticmethod
    def recover_from_log(job: str, run_id: str, lines: list[str], options: dict | None = None) -> JobStatus | None:
        """Use an explicit job completion marker to reconcile detached completed exports.

        When an export process disconnects or the worker daemon restarts, completed Data Pump
        jobs disappear from DBA_DATAPUMP_JOBS. This function parses the recorded log file lines
        to verify whether the job finished successfully, finished with errors, or halted fatally,
        recovering the final status and all produced dump pieces without remote database state.

        Args:
            job (str): Oracle Data Pump job name identifier.
            run_id (str): Unique export run identifier.
            lines (list[str]): Data Pump log file lines captured from remote Oracle server.
            options (dict | None): Optional execution snapshot options dictionary.

        Returns:
            JobStatus | None: Reconstructed JobStatus if completion marker was found, or None.
        """
        escaped_job = f'"{job}"'
        completion = None
        markers = ("successfully completed", "completed with", "stopped due to fatal error")

        # Search backwards from the end of the log to find the definitive final job marker.
        for line in reversed(lines):
            has_job = escaped_job in line
            has_marker = any(m in line for m in markers)
            if has_job and has_marker:
                completion = line
                break

        # If no recognized terminal marker is present, the job may still be active or aborted.
        if not completion:
            return None

        # Parse Oracle server errors (ORA-) and Data Pump utility errors (UDE-).
        errors = [line for line in lines if "ORA-" in line or "UDE-" in line]

        # Map terminal log phrase to internal JobStatus state string.
        is_fatal = "stopped due to fatal error" in completion
        state = "STOPPED" if is_fatal else "COMPLETED"

        # If Data Pump reports completion with errors but no ORA-/UDE- was extracted, use summary.
        if "completed with" in completion and not errors:
            errors = [completion]

        # Extract all generated dump pieces matching this run's prefix via bounded regex.
        files = []
        raw_prefix = OracleDatabase.file_prefix(run_id=run_id, options=options)
        prefix = re.escape(pattern=raw_prefix)
        pattern = rf"(?<![A-Za-z0-9_+-]){prefix}_\d+\.dmp(?![A-Za-z0-9_.+-])"
        for line in lines:
            matched_pieces = re.findall(pattern=pattern, string=line)
            files.extend(matched_pieces)

        # Calculate status properties with decomposed single-line definitions.
        percent = 100 if state == "COMPLETED" else None
        error_count = len(errors)
        recent_lines = lines[-100:]
        status = JobStatus(
            state=state,
            progress=percent,
            error_count=error_count,
            errors=errors,
            messages=recent_lines,
            files=files,
        )
        return status

    # Remote directory and file operations

    def verify_directory(self, directory: str) -> None:
        """Write, read, and remove a unique probe file in the selected DIRECTORY.

        Validates both read and write operating system permissions for the Oracle
        instance on the underlying filesystem path represented by the DIRECTORY object.

        Args:
            directory (str): Oracle DIRECTORY object name to verify.

        Raises:
            oracledb.DatabaseError: If writing, reading, or removing the probe file fails.
        """
        # Generate a unique probe filename using 24 hex characters of randomness.
        probe = f"oraexport_probe_{secrets.token_hex(nbytes=12)}.tmp"
        clean_dir = identifier(value=directory)
        self.oracle_cursor.execute(
            """
            DECLARE f UTL_FILE.FILE_TYPE; line VARCHAR2(100);
            BEGIN
              -- Verify write permissions by creating and closing probe file.
              f := UTL_FILE.FOPEN(:directory, :filename, 'w');
              UTL_FILE.PUT_LINE(f, 'oraexport'); UTL_FILE.FCLOSE(f);
              -- Verify read permissions by opening and retrieving line from probe file.
              f := UTL_FILE.FOPEN(:directory, :filename, 'r');
              UTL_FILE.GET_LINE(f, line); UTL_FILE.FCLOSE(f);
              -- Remove probe file to avoid leaving residual files on target storage.
              UTL_FILE.FREMOVE(:directory, :filename);
            EXCEPTION WHEN OTHERS THEN
              IF UTL_FILE.IS_OPEN(f) THEN UTL_FILE.FCLOSE(f); END IF;
              BEGIN UTL_FILE.FREMOVE(:directory, :filename); EXCEPTION WHEN OTHERS THEN NULL; END;
              RAISE;
            END;
        """,
            directory=clean_dir,
            filename=probe,
        )

    def read_log(self, directory: str, filename: str) -> list[str]:
        """Read a bounded tail of the recorded log for completion recovery.

        Uses UTL_FILE with a rolling 100KB buffer to capture the end of the log
        (where Data Pump logs completion status) without loading massive log files into memory.

        Args:
            directory (str): Oracle DIRECTORY object name containing the log.
            filename (str): Name of the Data Pump log file to read.

        Returns:
            list[str]: Bounded tail lines of the export log file.

        Raises:
            oracledb.DatabaseError: If opening or reading the file fails.
        """
        # Allocate output CLOB variable to receive bounded log content from PL/SQL.
        output = self.oracle_cursor.var(oracledb.DB_TYPE_CLOB)
        self.oracle_cursor.execute(
            """
            DECLARE f UTL_FILE.FILE_TYPE; line VARCHAR2(32767); content CLOB := '';
            BEGIN
              -- Open file in read mode with maximum line buffer size of 32767 bytes.
              f := UTL_FILE.FOPEN(:directory, :filename, 'r', 32767);
              LOOP
                BEGIN UTL_FILE.GET_LINE(f, line);
                EXCEPTION WHEN NO_DATA_FOUND THEN EXIT; END;
                content := content || line || CHR(10);
                -- Maintain a bounded rolling window of the trailing 100KB of log output.
                IF DBMS_LOB.GETLENGTH(content) > 200000 THEN
                  content := SUBSTR(content, -100000);
                END IF;
              END LOOP;
              UTL_FILE.FCLOSE(f); :output := content;
            EXCEPTION WHEN OTHERS THEN
              IF UTL_FILE.IS_OPEN(f) THEN UTL_FILE.FCLOSE(f); END IF; RAISE;
            END;
        """,
            directory=directory,
            filename=filename,
            output=output,
        )
        return OracleUtilities.lob_lines(variable=output)

    def file_size(self, directory: str, filename: str) -> int | None:
        """Measure an exact filename; return None when it is absent.

        Calls UTL_FILE.FGETATTR to check file existence and byte length non-destructively.

        Args:
            directory (str): Oracle DIRECTORY object name.
            filename (str): Name of the file to measure.

        Returns:
            int | None: File size in bytes if file exists; None if file is absent.
        """
        # Allocate output NUMBER variable for file length in bytes.
        size = self.oracle_cursor.var(oracledb.NUMBER)
        self.oracle_cursor.execute(
            """
            DECLARE ex BOOLEAN; len NUMBER; block BINARY_INTEGER;
            BEGIN
              -- FGETATTR reads file metadata without opening the file stream.
              UTL_FILE.FGETATTR(:directory, :filename, ex, len, block);
              IF ex THEN :size := len; ELSE :size := NULL; END IF;
            END;
        """,
            directory=directory,
            filename=filename,
            size=size,
        )
        raw_size = size.getvalue()
        return int(raw_size) if raw_size is not None else None

    def remove_file(self, directory: str, filename: str) -> None:
        """Remove one recorded file; already-absent files are idempotently complete.

        Verifies that file exists before attempting removal, ensuring idempotent cleanup.

        Args:
            directory (str): Oracle DIRECTORY object name.
            filename (str): Name of the file to delete.
        """
        # Only invoke FREMOVE if the file currently exists to prevent ORA-29283 errors.
        current_size = self.file_size(directory, filename)
        if current_size is not None:
            self.oracle_cursor.callproc(
                name="UTL_FILE.FREMOVE",
                keyword_parameters={"location": directory, "filename": filename},
            )

    # Export filenames and ownership

    @staticmethod
    def _filename_component(value: str) -> str:
        """Normalize a display name to a bounded, portable filename component.

        Replaces non-alphanumeric characters with hyphens, trims leading and trailing
        punctuation, bounds the result to 40 characters, and falls back to 'UNKNOWN'
        if the resulting string is empty.

        Args:
            value (str): Display name or raw identifier to normalize.

        Returns:
            str: Bounded, sanitized filename component safe for filesystem paths.
        """
        # Replace any character outside the safe set [A-Za-z0-9_-] with a hyphen.
        sanitized = re.sub(pattern=r"[^A-Za-z0-9_-]+", repl="-", string=value)
        # Strip leading/trailing punctuation and cap length at 40 characters.
        stripped = sanitized.strip("-_")[:40]
        # Invariant: Never allow an empty component to prevent double underscores.
        component = stripped or "UNKNOWN"
        return component

    @staticmethod
    def _readable_prefix(
        created_at: datetime,
        database: str,
        pdb: str | None,
        schemas: list[str],
        export_mode: str,
        run_id: str,
        id_length: int = 6,
    ) -> str:
        """Build a date-first prefix using UTC queue time and a short run identifier.

        Creates chronological, human-readable file namespaces that sort naturally on disk
        while remaining collision-free via the included unique run identifier.

        Args:
            created_at (datetime): UTC queue creation timestamp.
            database (str): Target database or registration name.
            pdb (str | None): Optional pluggable database name.
            schemas (list[str]): List of exported schema names.
            export_mode (str): Mode of export, either "FULL" or "SCHEMA".
            run_id (str): Unique export run identifier.
            id_length (int): Length of trailing run ID substring to append.

        Returns:
            str: Deterministic, chronological file prefix.
        """
        # For full database exports, use 'FULL'; otherwise join up to two schema names.
        if export_mode == "FULL":
            scope = "FULL"
        else:
            first_two = [OracleDatabase._filename_component(value=name) for name in schemas[:2]]
            scope = "+".join(first_two)

        # When more than two schemas are exported, append an indicator of the remaining count.
        if export_mode != "FULL" and len(schemas) > 2:
            extra_count = len(schemas) - 2
            scope += f"+{extra_count}more"

        # Assemble timestamp component (UTC) and database component.
        date_component = created_at.strftime(format="%Y-%m-%d_%H%M%S")
        db_component = OracleDatabase._filename_component(value=database)
        parts = [date_component, db_component]

        # Pluggable database component is optional for CDB or standalone non-CDB instances.
        if pdb:
            pdb_component = OracleDatabase._filename_component(value=pdb)
            parts.append(pdb_component)

        # Append scope summary and unique run identifier slice.
        short_id = run_id[:id_length]
        all_parts = [*parts, scope, short_id]
        result = "_".join(all_parts)
        return result

    @staticmethod
    def file_prefix(run_id: str, options: dict | None = None) -> str:
        """Resolve saved filenames, retaining the namespace of legacy runs.

        Validates that any stored custom file prefix adheres to strict length, character,
        and trailing run ID invariants to prevent path traversal and collision.

        Args:
            run_id (str): Unique export run identifier.
            options (dict | None): Optional execution snapshot options dictionary.

        Returns:
            str: Resolved immutable file prefix string.

        Raises:
            ValueError: If the configured file prefix fails validation rules.
        """
        opts = options or {}
        prefix = opts.get("file_prefix")
        # Legacy runs omit file_prefix; fallback to standard deterministic default.
        if prefix is None:
            legacy_prefix = f"oe_{run_id}"
            return legacy_prefix

        # Invariants: must be string, <= 230 chars (leaving buffer for _%u.dmp piece suffixes
        # within Oracle's 255-character maximum filename length limit), and alphanumeric chars only.
        is_valid_type = isinstance(prefix, str)
        is_valid_len = len(prefix) <= 230 if is_valid_type else False
        is_valid_chars = bool(re.fullmatch(pattern=r"[A-Za-z0-9_+-]+", string=prefix)) if is_valid_type else False

        # Invariant: End with the run_id (or a valid prefix of it) to guarantee ownership.
        suffixes = ["_" + run_id[:length] for length in range(6, 33, 2)]
        has_valid_suffix = any(prefix.endswith(s) for s in suffixes) if is_valid_type else False

        if not (is_valid_type and is_valid_len and is_valid_chars and has_valid_suffix):
            raise ValueError("Invalid export file prefix")

        return prefix

    @staticmethod
    def owned_filename(run_id: str, path: str, options: dict | None = None) -> str | None:
        """Accept only exact basenames from this run's unique file namespace.

        Validates that a file's basename strictly matches the Data Pump piece naming
        pattern or log filename corresponding to this specific run identifier, preventing
        accidental deletion or tracking of unowned Oracle files.

        Args:
            run_id (str): Unique export run identifier.
            path (str): Candidate path or filename from Oracle remote directory.
            options (dict | None): Optional options dictionary containing file_prefix.

        Returns:
            str | None: The verified filename basename if owned by the run, or None otherwise.
        """
        # PureWindowsPath handles both forward and backward slashes across OS platforms.
        name = PureWindowsPath(path).name
        prefix = OracleDatabase.file_prefix(run_id=run_id, options=options)
        escaped_prefix = re.escape(pattern=prefix)

        # Invariant: Data Pump dump pieces follow <prefix>_<piece>.dmp pattern.
        dmp_pattern = rf"{escaped_prefix}_(\d+)\.dmp"
        is_dump_match = bool(re.fullmatch(pattern=dmp_pattern, string=name))
        if is_dump_match:
            return name

        # Invariant: Data Pump export log file follows <prefix>.log pattern.
        expected_log = f"{prefix}.log"
        if name == expected_log:
            return name

        return None
