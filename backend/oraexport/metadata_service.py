"""Named Oracle metadata operations and domain-state mapping."""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import oracledb
from oracledb import Connection

from oraexport.config import get_settings
from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.application_error import ApplicationError
from oraexport.models.audit_event import AuditEvent
from oraexport.models.backup_file import BackupFile
from oraexport.models.credential import Credential
from oraexport.models.defaults import now
from oraexport.models.delivery import Delivery
from oraexport.models.discovered_pdb import DiscoveredPdb
from oraexport.models.discovered_service import DiscoveredService
from oraexport.models.discovery_response import DiscoveryResponse
from oraexport.models.export_run import ExportRun
from oraexport.models.lease import Lease
from oraexport.models.login_session import LoginSession
from oraexport.models.password_change_input import PasswordChangeInput
from oraexport.models.registration import Registration
from oraexport.models.registration_input import RegistrationInput
from oraexport.models.registration_update import RegistrationUpdate
from oraexport.models.run import Run
from oraexport.models.run_state import ACTIVE, BUSY, TERMINAL
from oraexport.models.summary_report import SummaryReport
from oraexport.models.target import Target
from oraexport.models.target_input import TargetInput
from oraexport.models.task import Task
from oraexport.models.user import User
from oraexport.models.user_input import UserInput
from oraexport.oracle_utilities import OracleUtilities
from oraexport.types import OracleConnectionFactory


class MetadataService:
    """Own metadata reads and writes within the caller's explicit transaction.

    Encapsulates queries, concurrency slot checks, row-locking operations,
    account, registration, and target workflows, and domain-state mapping.
    """

    # Initialization and connectivity

    def __init__(self, db: Connection, connection_factory: OracleConnectionFactory | None = None) -> None:
        """Use the caller's explicit Oracle transaction.

        Args:
            db (Connection): Caller-owned Oracle metadata transaction.
            connection_factory (OracleConnectionFactory | None): Remote driver connection factory.
        """
        # Retain the transactional session boundary for all repository operations.
        self.db = db
        self.connection_factory = connection_factory or OracleUtilities.connect

    def health(self) -> None:
        """Verify metadata connectivity."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT 1 FROM dual")

    # Users and authentication

    def users(self) -> list[User]:
        """List local accounts in username order.

        Returns:
            list[User]: Iterable of User records sorted alphabetically by username.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, username, password_hash, role, enabled FROM app_users ORDER BY username", {})
            query_rows = [User(id=row[0], username=row[1], password_hash=row[2], role=row[3], enabled=row[4]) for row in cursor.fetchall()]
        return query_rows

    def user_count(self) -> int:
        """Count local accounts for initial administrator bootstrapping."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM app_users", {})
            query_rows = cursor.fetchall()
        return query_rows[0][0]

    def user_by_username(self, username: str) -> User | None:
        """Find an account for authentication."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, username, password_hash, role, enabled FROM app_users WHERE username = :username", {"username": username})
            rows = [User(id=row[0], username=row[1], password_hash=row[2], role=row[3], enabled=row[4]) for row in cursor.fetchall()]
        return rows[0] if rows else None

    def create_user(self, body: UserInput, user: User) -> User:
        """Create a local account with an explicit role.

        Args:
            body (UserInput): Input payload containing username, password, and assigned role.
            user (User): Authenticated administrative user executing the creation action.

        Returns:
            User: Newly created and persisted user record.
        """
        # Hash plain-text password using argon2 before constructing user record.
        raw_secret = body.password.get_secret_value()
        hashed_password = EncryptionSerivce.hash_password(value=raw_secret)
        account = User(username=body.username, password_hash=hashed_password, role=body.role)
        with self.db.cursor() as cursor:
            parameters = {
                "id": account.id,
                "username": account.username,
                "password_hash": account.password_hash,
                "role": account.role,
                "enabled": int(account.enabled),
            }
            cursor.execute(
                ("INSERT INTO app_users (id, username, password_hash, role, enabled) VALUES (:id, :username, :password_hash, :role, :enabled)"),
                parameters,
            )

        # Stage audit record referencing generated user id before committing.
        self.record_audit(user=user, action="user_created", resource_id=account.id)
        self.db.commit()
        return account

    def disable_user(self, user_id: str, user: User) -> User:
        """Disable another account and invalidate its effective access.

        Args:
            user_id (str): Unique identifier of the target user account to disable.
            user (User): Authenticated administrative user executing the disable action.

        Returns:
            User: Updated user record with enabled flag set to False.

        Raises:
            ValueError: If an administrator attempts to disable their own account.
        """
        # Prevent self-lockout by forbidding administrators from disabling themselves.
        if user_id == user.id:
            raise ValueError("Cannot disable your own account")
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, username, password_hash, role, enabled FROM app_users WHERE id = :record_key", {"record_key": user_id})
            rows = [User(id=row[0], username=row[1], password_hash=row[2], role=row[3], enabled=row[4]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        account = rows[0]
        account.enabled = False
        with self.db.cursor() as cursor:
            parameters = {"enabled": int(account.enabled), "record_key": account.id}
            cursor.execute(
                ("UPDATE app_users SET enabled = :enabled WHERE id = :record_key"),
                parameters,
            )
        # Stage audit entry and commit transaction boundary atomically.
        self.record_audit(user=user, action="user_disabled", resource_id=user_id)
        self.db.commit()
        return account

    def login(self, username: str, password: str) -> tuple[User, str]:
        """Authenticate and commit a new hashed session token.

        Args:
            username (str): Account username submitted during login.
            password (str): Plain-text password to verify against stored password hash.

        Returns:
            tuple[User, str]: Two-element tuple containing authenticated user record and raw token.

        Raises:
            ApplicationError: If account is disabled or credentials fail verification.
        """
        # Look up user by username in metadata repository.
        user = self.user_by_username(username=username)
        if not user or not user.enabled or not EncryptionSerivce.verify_password(value=password, digest=user.password_hash):
            raise ApplicationError(status_code=401, detail="Invalid username or password")
        # Generate raw random session token and its cryptographic digest.
        session_pair = EncryptionSerivce.new_session_token()
        token = session_pair[0]
        digest = session_pair[1]
        session_duration = timedelta(hours=get_settings().session_hours)
        expires_at = now() + session_duration
        login_session = LoginSession(id=digest, user_id=user.id, expires_at=expires_at)
        # Persist hashed session token and record audit trail within transaction boundary.
        with self.db.cursor() as cursor:
            parameters = {
                "id": login_session.id,
                "user_id": login_session.user_id,
                "expires_at": login_session.expires_at,
            }
            cursor.execute(
                ("INSERT INTO login_sessions (id, user_id, expires_at) VALUES (:id, :user_id, :expires_at)"),
                parameters,
            )
        self.record_audit(user=user, action="login", resource_id=user.id)
        self.db.commit()
        return user, token

    def change_password(self, body: PasswordChangeInput, user: User, token: str) -> None:
        """Verify current credentials and atomically replace the password and revoke other sessions.

        Args:
            body (PasswordChangeInput): Current and replacement password secrets.
            user (User): Authenticated account whose password is being changed.
            token (str): Current browser session token to preserve.
        """
        # Serialize credential changes and verify the latest hash rather than the dependency snapshot.
        with self.db.cursor() as cursor:
            cursor.execute(
                statement="SELECT password_hash, enabled FROM app_users WHERE id = :user_id FOR UPDATE",
                parameters={"user_id": user.id},
            )
            rows = cursor.fetchall()
        if not rows or not rows[0][1]:
            raise ApplicationError(status_code=401, detail="Login required")
        current_password = body.current_password.get_secret_value()
        if not EncryptionSerivce.verify_password(value=current_password, digest=rows[0][0]):
            raise ApplicationError(status_code=400, detail="Current password is incorrect")
        new_password = body.new_password.get_secret_value()
        if new_password == current_password:
            raise ApplicationError(status_code=400, detail="New password must be different from your current password")
        password_hash = EncryptionSerivce.hash_password(value=new_password)
        with self.db.cursor() as cursor:
            cursor.execute(
                statement="UPDATE app_users SET password_hash = :password_hash WHERE id = :user_id",
                parameters={"password_hash": password_hash, "user_id": user.id},
            )
            cursor.execute(
                statement="DELETE FROM login_sessions WHERE user_id = :user_id AND id <> :session_id",
                parameters={"user_id": user.id, "session_id": EncryptionSerivce.token_hash(token=token)},
            )
        self.record_audit(user=user, action="password_changed", resource_id=user.id)
        self.db.commit()

    def logout(self, token: str) -> None:
        """Invalidate a persisted browser session in an explicit transaction.

        Args:
            token (str): Raw opaque bearer session token to invalidate and remove.

        Returns:
            None: No value is returned upon successful session invalidation.
        """
        # Hash token before lookup to avoid timing leaks against stored digests.
        session_key = EncryptionSerivce.token_hash(token=token)
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, user_id, expires_at FROM login_sessions WHERE id = :record_key", {"record_key": session_key})
            rows = [LoginSession(id=row[0], user_id=row[1], expires_at=row[2]) for row in cursor.fetchall()]
        session = rows[0] if rows else None
        if session:
            with self.db.cursor() as cursor:
                parameters = {"record_key": session.id}
                cursor.execute(
                    ("DELETE FROM login_sessions WHERE id = :record_key"),
                    parameters,
                )
        self.db.commit()

    # Database registrations

    def registrations(self) -> list[Registration]:
        """List database registrations in display order.

        Returns:
            list[Registration]: Iterable of Registration records sorted by name.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, name, dsn, credential_id, kind, version FROM registrations ORDER BY name", {})
            query_rows = [Registration(id=row[0], name=row[1], dsn=row[2], credential_id=row[3], kind=row[4], version=row[5]) for row in cursor.fetchall()]
        return query_rows

    def create_registration(self, body: RegistrationInput, user: User) -> Registration:
        """Save an encrypted DBA credential and database registration.

        Args:
            body (RegistrationInput): Input payload containing name, DSN, username, password.
            user (User): Authenticated administrative user creating the registration.

        Returns:
            Registration: Newly created database registration record.
        """
        secret_val = body.password.get_secret_value()
        cipher = EncryptionSerivce.encrypt(value=secret_val)
        credential = Credential(username=body.username, ciphertext=cipher, sysdba=body.sysdba)
        with self.db.cursor() as cursor:
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

        registration = Registration(name=body.name, dsn=body.dsn, credential_id=credential.id)
        with self.db.cursor() as cursor:
            parameters = {
                "id": registration.id,
                "name": registration.name,
                "dsn": registration.dsn,
                "credential_id": registration.credential_id,
                "kind": registration.kind,
                "version": registration.version,
            }
            cursor.execute(
                ("INSERT INTO registrations (id, name, dsn, credential_id, kind, version) VALUES (:id, :name, :dsn, :credential_id, :kind, :version)"),
                parameters,
            )

        self.record_audit(user=user, action="database_registered", resource_id=registration.id)
        self.db.commit()
        return registration

    def discover_registration(self, registration_id: str, user: User) -> DiscoveryResponse:
        """Test the registration and discover container topology.

        Args:
            registration_id (str): Identifier of the database registration to discover.
            user (User): Authenticated user requesting topology discovery.

        Returns:
            DiscoveryResponse: Topology details detailing database kind, version, and PDB services.
        """
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key",
                {"record_key": registration_id},
            )
            rows = [Registration(id=row[0], name=row[1], dsn=row[2], credential_id=row[3], kind=row[4], version=row[5]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        registration = rows[0]
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": registration.credential_id},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        admin_cred = rows[0] if rows else None
        if admin_cred is None:
            raise ValueError("Required credential record is missing")
        from oraexport.oracle_database import OracleDatabase

        database = OracleDatabase(target_id=None, session=self.db, connection_factory=self.connection_factory)
        with database.connected(dsn=registration.dsn, credential=admin_cred):
            discovery = database.discover()
        base_dsn = registration.dsn
        pdbs = [
            DiscoveredPdb(
                name=pdb.name,
                open_mode=pdb.open_mode,
                services=[service if isinstance(service, DiscoveredService) else DiscoveredService(name=service, dsn=OracleUtilities.parse_dsn(dsn=base_dsn, service_name=service)) for service in pdb.services],
            )
            for pdb in discovery.pdbs
        ]
        registration.kind = discovery.kind
        registration.version = discovery.version
        with self.db.cursor() as cursor:
            parameters = {
                "kind": registration.kind,
                "version": registration.version,
                "record_key": registration.id,
            }
            cursor.execute(
                ("UPDATE registrations SET kind = :kind, version = :version WHERE id = :record_key"),
                parameters,
            )
        self.record_audit(user=user, action="database_discovered", resource_id=registration_id)
        self.db.commit()
        return DiscoveryResponse(kind=discovery.kind, version=discovery.version, container_name=discovery.container_name, edition=discovery.edition, pdbs=pdbs)

    def update_registration(self, registration_id: str, body: RegistrationUpdate, user: User) -> Registration:
        """Update a registration and inherited DBA credentials without changing run secrets.

        Args:
            registration_id (str): Identifier of the existing database registration.
            body (RegistrationUpdate): Updated fields and optional replacement password.
            user (User): Authenticated user performing the update.

        Returns:
            Registration: Updated registration record.
        """
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key FOR UPDATE",
                {"record_key": registration_id},
            )
            rows = [Registration(id=row[0], name=row[1], dsn=row[2], credential_id=row[3], kind=row[4], version=row[5]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        registration = rows[0]
        connection_changed = registration.dsn != body.dsn or body.password is not None
        registration.name = body.name
        registration.dsn = body.dsn
        with self.db.cursor() as cursor:
            parameters = {
                "name": registration.name,
                "dsn": registration.dsn,
                "record_key": registration.id,
            }
            cursor.execute(
                ("UPDATE registrations SET name = :name, dsn = :dsn WHERE id = :record_key"),
                parameters,
            )
        if body.password is not None:
            previous_id = registration.credential_id
            secret_val = body.password.get_secret_value()
            cipher = EncryptionSerivce.encrypt(value=secret_val)
            assert body.username is not None
            assert body.sysdba is not None
            credential = Credential(username=body.username, ciphertext=cipher, sysdba=body.sysdba)
            with self.db.cursor() as cursor:
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

            registration.credential_id = credential.id
            with self.db.cursor() as cursor:
                parameters = {
                    "credential_id": registration.credential_id,
                    "record_key": registration.id,
                }
                cursor.execute(
                    ("UPDATE registrations SET credential_id = :credential_id WHERE id = :record_key"),
                    parameters,
                )
            for target in self.inherited_targets(registration_id=registration_id, credential_id=previous_id):
                target.admin_credential_id = credential.id
                with self.db.cursor() as cursor:
                    parameters = {
                        "admin_credential_id": target.admin_credential_id,
                        "record_key": target.id,
                    }
                    cursor.execute(
                        ("UPDATE targets SET admin_credential_id = :admin_credential_id WHERE id = :record_key"),
                        parameters,
                    )

            self.remove_unused_credentials(credential_ids={previous_id})
        if connection_changed:
            registration.kind = "unknown"
            registration.version = None
            with self.db.cursor() as cursor:
                parameters = {
                    "kind": registration.kind,
                    "version": registration.version,
                    "record_key": registration.id,
                }
                cursor.execute(
                    ("UPDATE registrations SET kind = :kind, version = :version WHERE id = :record_key"),
                    parameters,
                )
        self.record_audit(user=user, action="database_updated", resource_id=registration_id)
        self.db.commit()
        return registration

    def delete_registration(self, registration_id: str, user: User) -> dict[str, bool]:
        """Remove an unused registration and its unshared encrypted credential.

        Args:
            registration_id (str): Identifier of the database registration to remove.
            user (User): Authenticated user performing the removal.

        Returns:
            dict[str, bool]: Dictionary containing ok status flag.

        Raises:
            ApplicationError: If targets still depend on this registration (HTTP 409).
        """
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key FOR UPDATE",
                {"record_key": registration_id},
            )
            rows = [Registration(id=row[0], name=row[1], dsn=row[2], credential_id=row[3], kind=row[4], version=row[5]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        registration = rows[0]
        if self.registration_has_targets(registration_id=registration_id):
            raise ApplicationError(status_code=409, detail="Remove this database's export targets first")
        credential_id = registration.credential_id
        with self.db.cursor() as cursor:
            parameters = {"record_key": registration.id}
            cursor.execute(
                ("DELETE FROM registrations WHERE id = :record_key"),
                parameters,
            )

        self.remove_unused_credentials(credential_ids={credential_id})
        self.record_audit(user=user, action="database_removed", resource_id=registration_id)
        self.db.commit()
        return {"ok": True}

    def registration_has_targets(self, registration_id: str) -> bool:
        """Check whether a registration is still referenced."""
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM targets WHERE registration_id = :registration_id",
                {"registration_id": registration_id},
            )
            query_rows = cursor.fetchall()
        return bool(query_rows)

    # Export targets

    def targets(self) -> list[Target]:
        """List target services in display order.

        Returns:
            list[Target]: Iterable of Target service records sorted by name.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets ORDER BY name", {})
            query_rows = [Target(id=row[0], registration_id=row[1], name=row[2], dsn=row[3], admin_credential_id=row[4], export_credential_id=row[5], directory=row[6], tablespace=row[7], provisioned=row[8], edition=row[9], container_name=row[10]) for row in cursor.fetchall()]
        return query_rows

    def target_ids(self) -> list[str]:
        """List database owners to visit for retention cleanup."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id FROM targets", {})
            query_rows = cursor.fetchall()
        return [row[0] for row in query_rows]

    def create_target(self, body: TargetInput, user: User) -> Target:
        """Validate a PDB/non-CDB service and save its administrative credential.

        Args:
            body (TargetInput): Target creation payload specifying service DSN and credentials.
            user (User): Authenticated user creating the target.

        Returns:
            Target: Newly created target service record.

        Raises:
            ValueError: If credentials are incomplete or service connects to a mismatched PDB.
        """
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key FOR UPDATE",
                {"record_key": body.registration_id},
            )
            rows = [Registration(id=row[0], name=row[1], dsn=row[2], credential_id=row[3], kind=row[4], version=row[5]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        registration = rows[0]
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key",
                {"record_key": registration.credential_id},
            )
            rows = [Credential(id=row[0], username=row[1], ciphertext=row[2].read() if hasattr(row[2], "read") else row[2], sysdba=row[3]) for row in cursor.fetchall()]
        credential = rows[0] if rows else None
        if credential is None:
            raise ValueError("Required credential record is missing")
        if body.username is not None or body.password is not None:
            if not body.username or not body.password:
                raise ValueError("Supply both username and password, or inherit registration credentials")
            secret_val = body.password.get_secret_value()
            cipher = EncryptionSerivce.encrypt(value=secret_val)
            assert body.username is not None
            assert body.sysdba is not None
            credential = Credential(username=body.username, ciphertext=cipher, sysdba=body.sysdba)
            with self.db.cursor() as cursor:
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

        from oraexport.oracle_database import OracleDatabase

        database = OracleDatabase(target_id=None, session=self.db, connection_factory=self.connection_factory)
        with database.connected(dsn=body.dsn, credential=credential):
            catalog = database.discover_catalog()
        if body.pdb_name is not None and catalog.container_name != body.pdb_name:
            raise ValueError("The service connects to a different PDB; select the discovered PDB service")
        target = Target(registration_id=registration.id, name=body.name, dsn=body.dsn, admin_credential_id=credential.id, container_name=catalog.container_name, edition=catalog.edition)
        with self.db.cursor() as cursor:
            parameters = {
                "id": target.id,
                "registration_id": target.registration_id,
                "name": target.name,
                "dsn": target.dsn,
                "admin_credential_id": target.admin_credential_id,
                "export_credential_id": target.export_credential_id,
                "directory": target.directory,
                "tablespace": target.tablespace,
                "provisioned": int(target.provisioned),
                "edition": target.edition,
                "container_name": target.container_name,
            }
            cursor.execute(
                (
                    "INSERT INTO targets (id, registration_id, name, dsn, "
                    "admin_credential_id, export_credential_id, directory, "
                    "tablespace, provisioned, edition, container_name) "
                    "VALUES (:id, :registration_id, :name, :dsn, "
                    ":admin_credential_id, :export_credential_id, "
                    ":directory, :tablespace, :provisioned, :edition, "
                    ":container_name)"
                ),
                parameters,
            )

        self.record_audit(user=user, action="target_created", resource_id=target.id)
        self.db.commit()
        return target

    def delete_target(self, target_id: str, user: User) -> dict[str, bool]:
        """Remove a target only when export policies and history do not depend on it.

        Args:
            target_id (str): Identifier of the target service to delete.
            user (User): Authenticated user performing deletion.

        Returns:
            dict[str, bool]: Dictionary containing ok status flag.

        Raises:
            ApplicationError: If export tasks or run history depend on this target (HTTP 409).
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key FOR UPDATE", {"record_key": target_id})
            rows = [Target(id=row[0], registration_id=row[1], name=row[2], dsn=row[3], admin_credential_id=row[4], export_credential_id=row[5], directory=row[6], tablespace=row[7], provisioned=row[8], edition=row[9], container_name=row[10]) for row in cursor.fetchall()]
        if not rows:
            raise ApplicationError(status_code=404, detail="Resource not found")
        target = rows[0]
        if self.target_has_history(target_id=target_id):
            raise ApplicationError(status_code=409, detail="Cannot remove a target with export tasks or run history")
        credential_ids = {target.admin_credential_id}
        if target.export_credential_id:
            credential_ids.add(target.export_credential_id)
        with self.db.cursor() as cursor:
            parameters = {"record_key": target.id}
            cursor.execute(
                ("DELETE FROM targets WHERE id = :record_key"),
                parameters,
            )

        self.remove_unused_credentials(credential_ids=credential_ids)
        self.record_audit(user=user, action="target_removed", resource_id=target_id)
        self.db.commit()
        return {"ok": True}

    def inherited_targets(self, registration_id: str, credential_id: str) -> list[Target]:
        """Lock targets inheriting the registration's previous credential."""
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE registration_id = :registration AND admin_credential_id = :credential FOR UPDATE"),
                {"registration": registration_id, "credential": credential_id},
            )
            query_rows = [Target(id=row[0], registration_id=row[1], name=row[2], dsn=row[3], admin_credential_id=row[4], export_credential_id=row[5], directory=row[6], tablespace=row[7], provisioned=row[8], edition=row[9], container_name=row[10]) for row in cursor.fetchall()]
        return query_rows

    def target_has_history(self, target_id: str) -> bool:
        """Check whether tasks or execution history prevent target deletion."""
        binds = {"target_id": target_id}
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id FROM tasks WHERE target_id = :target_id", binds or {})
            tasks = cursor.fetchall()
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id FROM runs WHERE target_id = :target_id", binds or {})
            runs = cursor.fetchall()
        return bool(tasks or runs)

    # Tasks and scheduling

    def tasks(self) -> list[Task]:
        """List export policies in display order.

        Returns:
            list[Task]: Iterable of Task export policy records sorted by name.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks ORDER BY name", {})
            query_rows = [
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
        return query_rows

    def task_ids(self, target_id: str) -> list[str]:
        """List policies owned by a database."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id FROM tasks WHERE target_id = :p0", {"p0": target_id})
            query_rows = cursor.fetchall()
        return [row[0] for row in query_rows]

    def due_tasks(self, current: datetime) -> list[Task]:
        """Lock enabled policies whose next occurrence is due."""
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT id, name, target_id, schema_name, directory, enabled, cron, timezone, retention_days, parallel, file_size_gb, content, consistent_snapshot, next_run_at, export_mode, schema_names FROM tasks WHERE enabled = 1 AND next_run_at <= :p0 FOR UPDATE"),
                {"p0": current},
            )
            query_rows = [
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
        return query_rows

    def lease_for_update(self) -> Lease | None:
        """Lock the singleton worker lease."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT name, owner, expires_at, last_cleanup_at FROM worker_leases WHERE name = :name FOR UPDATE", {"name": "scheduler"})
            rows = [Lease(name=row[0], owner=row[1], expires_at=row[2], last_cleanup_at=row[3]) for row in cursor.fetchall()]
        return rows[0] if rows else None

    # Run history and reporting

    def runs(
        self,
        state: str | None = None,
        task_id: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Run]:
        """Read execution history using explicit filters and named Oracle binds.

        Args:
            state (str | None): Run state or 'active' for monitored executions.
            task_id (str | None): Task identifier to match.
            target_id (str | None): Target identifier to match.
            since (datetime | None): Inclusive creation-time lower bound.
            until (datetime | None): Inclusive creation-time upper bound.
            offset (int): Number of matching records to skip.
            limit (int | None): Maximum number of records, or None for all.

        Returns:
            list[Run]: Records ordered by creation time descending.
        """
        if state and state not in BUSY | TERMINAL | {"active"}:
            raise ValueError("Unknown run state")
        if since is not None and since.tzinfo is not None:
            since = since.astimezone(tz=timezone.utc).replace(tzinfo=None)
        if until is not None and until.tzinfo is not None:
            until = until.astimezone(tz=timezone.utc).replace(tzinfo=None)
        sql = (
            "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"
            " WHERE (:state IS NULL OR state = :state"
            " OR (:state = 'active' AND state IN ('starting', 'running', 'reconciliation_required')))"
            " AND (:task_id IS NULL OR task_id = :task_id)"
            " AND (:target_id IS NULL OR target_id = :target_id)"
            " AND (:since IS NULL OR created_at >= :since)"
            " AND (:until IS NULL OR created_at <= :until)"
            " ORDER BY created_at DESC"
        )
        binds: dict[str, str | int | datetime | None] = {
            "state": state or None,
            "task_id": task_id or None,
            "target_id": target_id or None,
            "since": since,
            "until": until,
        }
        if offset:
            sql += " OFFSET :row_offset ROWS"
            binds["row_offset"] = offset
        if limit is not None:
            sql += " FETCH NEXT :row_limit ROWS ONLY"
            binds["row_limit"] = limit
        with self.db.cursor() as cursor:
            cursor.setinputsizes(since=oracledb.DB_TYPE_TIMESTAMP, until=oracledb.DB_TYPE_TIMESTAMP)
            cursor.execute(statement=sql, parameters=binds)
            query_rows = [
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
        return query_rows

    def run_count(
        self,
        state: str | None = None,
        task_id: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Count execution history using explicit filters and named Oracle binds.

        Args:
            state (str | None): Run state or 'active' for monitored executions.
            task_id (str | None): Task identifier to match.
            target_id (str | None): Target identifier to match.
            since (datetime | None): Inclusive creation-time lower bound.
            until (datetime | None): Inclusive creation-time upper bound.

        Returns:
            int: Number of matching run records.
        """
        if state and state not in BUSY | TERMINAL | {"active"}:
            raise ValueError("Unknown run state")
        if since is not None and since.tzinfo is not None:
            since = since.astimezone(tz=timezone.utc).replace(tzinfo=None)
        if until is not None and until.tzinfo is not None:
            until = until.astimezone(tz=timezone.utc).replace(tzinfo=None)
        sql = (
            "SELECT COUNT(*) FROM runs"
            " WHERE (:state IS NULL OR state = :state"
            " OR (:state = 'active' AND state IN ('starting', 'running', 'reconciliation_required')))"
            " AND (:task_id IS NULL OR task_id = :task_id)"
            " AND (:target_id IS NULL OR target_id = :target_id)"
            " AND (:since IS NULL OR created_at >= :since)"
            " AND (:until IS NULL OR created_at <= :until)"
        )
        binds = {
            "state": state or None,
            "task_id": task_id or None,
            "target_id": target_id or None,
            "since": since,
            "until": until,
        }
        with self.db.cursor() as cursor:
            cursor.setinputsizes(since=oracledb.DB_TYPE_TIMESTAMP, until=oracledb.DB_TYPE_TIMESTAMP)
            cursor.execute(statement=sql, parameters=binds)
            row = cursor.fetchone()
        return row[0]

    def run_batches(
        self,
        state: str | None = None,
        task_id: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        batch_size: int = 200,
    ) -> Iterator[list[Run]]:
        """Stream filtered history from a single cursor in bounded batches.

        Args:
            state (str | None): Run state or 'active' for monitored executions.
            task_id (str | None): Task identifier to match.
            target_id (str | None): Target identifier to match.
            since (datetime | None): Inclusive creation-time lower bound.
            until (datetime | None): Inclusive creation-time upper bound.
            batch_size (int): Maximum runs materialized at once.

        Yields:
            list[Run]: Detached history records.
        """
        if not 1 <= batch_size <= 1000:
            raise ValueError("Batch size must be between 1 and 1000")
        if state and state not in BUSY | TERMINAL | {"active"}:
            raise ValueError("Unknown run state")
        if since is not None and since.tzinfo is not None:
            since = since.astimezone(tz=timezone.utc).replace(tzinfo=None)
        if until is not None and until.tzinfo is not None:
            until = until.astimezone(tz=timezone.utc).replace(tzinfo=None)
        sql = (
            "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs"
            " WHERE (:state IS NULL OR state = :state"
            " OR (:state = 'active' AND state IN ('starting', 'running', 'reconciliation_required')))"
            " AND (:task_id IS NULL OR task_id = :task_id)"
            " AND (:target_id IS NULL OR target_id = :target_id)"
            " AND (:since IS NULL OR created_at >= :since)"
            " AND (:until IS NULL OR created_at <= :until)"
            " ORDER BY created_at DESC, id DESC"
        )
        binds = {
            "state": state or None,
            "task_id": task_id or None,
            "target_id": target_id or None,
            "since": since,
            "until": until,
        }
        with self.db.cursor() as cursor:
            cursor.arraysize = batch_size
            cursor.setinputsizes(since=oracledb.DB_TYPE_TIMESTAMP, until=oracledb.DB_TYPE_TIMESTAMP)
            cursor.execute(statement=sql, parameters=binds)
            while rows := cursor.fetchmany(size=batch_size):
                yield [
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
                    for row in rows
                ]

    def dump_sizes(self, run_ids: list[str]) -> dict[str, int | None]:
        """Measure a report batch with one file query, preserving unknown sizes.

        Args:
            run_ids (list[str]): At most 1000 run identifiers.

        Returns:
            dict[str, int | None]: Total dump bytes, or None if absent or unmeasured.
        """
        # Return empty mapping when no run identifiers are requested.
        if not run_ids:
            return {}
        # Enforce Oracle limit on IN-list expressions (maximum 1000 items).
        if len(run_ids) > 1000:
            raise ValueError("At most 1000 runs may be measured in one batch")
        # Generate named bind parameters for the IN clause.
        binds = {f"run_{i}": value for i, value in enumerate(run_ids)}
        placeholders = ", ".join(":" + key for key in binds)
        # Default all run IDs to None in the result map.
        result = dict.fromkeys(run_ids)
        unknown = set()
        # Fetch file sizes for all matching dump files in a single query.
        with self.db.cursor() as cursor:
            cursor.execute(
                f"SELECT run_id, size_bytes FROM backup_files WHERE kind = 'dump' AND run_id IN ({placeholders})",
                binds,
            )
            # Aggregate total size per run, noting runs with missing/unmeasured sizes.
            for run_id, size in cursor.fetchall():
                if size is None:
                    unknown.add(run_id)
                else:
                    result[run_id] = (result[run_id] or 0) + int(size)
        # Mark any run with at least one unmeasured file size as None.
        for run_id in unknown:
            result[run_id] = None
        return result

    def summary(self) -> SummaryReport:
        """Summarize run outcomes and measured retained dump bytes.

        Reads changes already written in the caller's transaction.

        Returns:
            SummaryReport: Aggregated summary containing run counts, retained bytes, and totals.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT state, COUNT(*) FROM runs GROUP BY state", {})
            query_rows = cursor.fetchall()
        counts = dict(query_rows)
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT SUM(size_bytes) FROM backup_files WHERE kind = :kind AND deleted_at IS NULL"),
                {"kind": "dump"},
            )
            query_rows = cursor.fetchall()
        retained_bytes = query_rows[0][0] or 0
        with self.db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM tasks", {})
            query_rows = cursor.fetchall()
        with self.db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM targets", {})
            query_rows_2 = cursor.fetchall()
        return SummaryReport(
            counts=counts,
            retained_bytes=retained_bytes,
            tasks=query_rows[0][0],
            targets=query_rows_2[0][0],
        )

    # Run coordination and execution state

    def get_for_update(self, run_id: str) -> Run | None:
        """Refresh a run under a row lock without a pagination clause.

        Args:
            run_id (str): Primary key of the run record to fetch and lock.

        Returns:
            Run | None: The locked Run record if found, or None.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :id FOR UPDATE", {"id": run_id})
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
        return rows[0] if rows else None

    def active(self) -> list[Run]:
        """Read all runs that currently reserve concurrency slots.

        Returns:
            list[Run]: List of active run records currently starting, running, or canceling.
        """
        # Query all records whose state occupies a global or target concurrency slot.
        state_parameters = {f"state_{index}": value for index, value in enumerate(sorted(ACTIVE))}
        state_placeholders = ", ".join(":" + name for name in state_parameters)
        with self.db.cursor() as cursor:
            cursor.execute(
                f"SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE state IN ({state_placeholders})", state_parameters
            )
            query_rows = [
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
        return query_rows

    def active_run_ids(self) -> list[str]:
        """List exports requiring worker monitoring."""
        state_parameters = {f"state_{index}": value for index, value in enumerate(sorted(ACTIVE))}
        state_placeholders = ", ".join(":" + name for name in state_parameters)
        with self.db.cursor() as cursor:
            cursor.execute(f"SELECT id FROM runs WHERE state IN ({state_placeholders})", state_parameters)
            query_rows = [tuple(value.read() if hasattr(value, "read") else value for value in row) for row in cursor.fetchall()]
        return [row[0] for row in query_rows]

    def pending_for_update(self) -> list[Run]:
        """Lock queued work in creation order, skipping locked rows.

        Returns:
            list[Run]: List of queued Run records locked in FIFO order with SKIP LOCKED.
        """
        # SKIP LOCKED allows parallel workers to claim queued jobs without blocking.
        with self.db.cursor() as cursor:
            cursor.execute(
                (
                    "SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE state = :p0 ORDER BY created_at FOR UPDATE SKIP LOCKED"
                ),
                {"p0": "queued"},
            )
            query_rows = [
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
        return query_rows

    def has_busy_task(self, task_id: str) -> bool:
        """Check whether a task already has queued, active, or uncertain work.

        Args:
            task_id (str): Primary key of the export policy task to evaluate.

        Returns:
            bool: True if any run for this task is currently queued or executing; False otherwise.
        """
        state_parameters = {f"state_{index}": value for index, value in enumerate(sorted(BUSY))}
        state_placeholders = ", ".join(":" + name for name in state_parameters)
        with self.db.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM runs WHERE task_id = :task_id AND state IN ({state_placeholders})",
                {"task_id": task_id} | state_parameters,
            )
            rows = [tuple(value.read() if hasattr(value, "read") else value for value in row) for row in cursor.fetchall()]
        return bool(rows)

    @staticmethod
    def execution(run: Run) -> ExportRun:
        """Decode execution state without leaking storage encoding into the domain.

        Args:
            run (Run): Run metadata record holding stored JSON log and error strings.

        Returns:
            ExportRun: Domain model containing deserialized log messages and execution state.
        """
        # Decode stored JSON strings into structured domain lists.
        parsed_errors = json.loads(s=run.errors)
        parsed_messages = json.loads(s=run.messages)
        return ExportRun(
            state=run.state,
            cancel_requested=run.cancel_requested,
            progress=run.progress,
            errors=parsed_errors,
            messages=parsed_messages,
            last_seen_at=run.last_seen_at,
            finished_at=run.finished_at,
        )

    def save_execution(self, run: Run, execution: ExportRun) -> None:
        """Persist execution state without committing the caller's transaction.

        Args:
            run (Run): Run metadata record being modified.
            execution (ExportRun): Domain model holding latest observed execution state.
        """
        run.state = execution.state
        run.cancel_requested = execution.cancel_requested
        run.progress = execution.progress
        run.last_seen_at = execution.last_seen_at
        run.finished_at = execution.finished_at
        # Serialize list fields into JSON strings for database CLOB storage.
        run.errors = json.dumps(obj=execution.errors)
        run.messages = json.dumps(obj=execution.messages)
        with self.db.cursor() as cursor:
            cursor.setinputsizes(errors=oracledb.DB_TYPE_CLOB, messages=oracledb.DB_TYPE_CLOB)
            parameters = {
                name: int(value) if isinstance(value, bool) else value
                for name, value in (
                    {
                        name: getattr(run, name)
                        for name in (
                            "state",
                            "cancel_requested",
                            "progress",
                            "last_seen_at",
                            "finished_at",
                            "errors",
                            "messages",
                        )
                    }
                    | {"record_key": run.id}
                ).items()
            }
            cursor.execute(
                ("UPDATE runs SET state = :state, cancel_requested = :cancel_requested, progress = :progress, last_seen_at = :last_seen_at, finished_at = :finished_at, errors = :errors, messages = :messages WHERE id = :record_key"),
                parameters,
            )

    # Export files and retention

    def files(self, run_id: str, *, retained: bool = False, lock: bool = False) -> list[BackupFile]:
        """Read an export manifest, optionally refreshing retained files under locks."""
        sql = "SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE run_id = :run_id"
        if retained:
            sql += " AND deleted_at IS NULL"
        if lock:
            sql += " ORDER BY id FOR UPDATE"
        with self.db.cursor() as cursor:
            cursor.execute(sql, {"run_id": run_id})
            query_rows = [BackupFile(id=row[0], run_id=row[1], directory=row[2], filename=row[3], kind=row[4], size_bytes=row[5], deleted_at=row[6], cleanup_attempts=row[7], cleanup_error=row[8].read() if hasattr(row[8], "read") else row[8]) for row in cursor.fetchall()]
        return query_rows

    def file_for_update(self, file_id: str) -> BackupFile | None:
        """Refresh a manifest entry under a lock before recording cleanup."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id, run_id, directory, filename, kind, size_bytes, deleted_at, cleanup_attempts, cleanup_error FROM backup_files WHERE id = :id FOR UPDATE", {"id": file_id})
            rows = [BackupFile(id=row[0], run_id=row[1], directory=row[2], filename=row[3], kind=row[4], size_bytes=row[5], deleted_at=row[6], cleanup_attempts=row[7], cleanup_error=row[8].read() if hasattr(row[8], "read") else row[8]) for row in cursor.fetchall()]
        return rows[0] if rows else None

    def filename_exists(self, filename: str) -> bool:
        """Reserve unique export filenames across historical manifests."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT id FROM backup_files WHERE filename = :filename", {"filename": filename})
            query_rows = cursor.fetchall()
        return bool(query_rows)

    def pending_master_cleanup(self, target_id: str) -> list[str]:
        """List terminal cleanup intents independently of file retention eligibility.

        Args:
            target_id (str): Database owner of the runs.

        Returns:
            list[str]: Pending run identifiers.
        """
        states = {f"state_{i}": state for i, state in enumerate(sorted(TERMINAL))}
        placeholders = ", ".join(":" + key for key in states)
        with self.db.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM runs WHERE target_id = :target_id AND master_cleanup_pending = 1 AND state IN ({placeholders})",
                {"target_id": target_id} | states,
            )
            return [row[0] for row in cursor.fetchall()]

    def expired_runs(self, task: Task, cutoff: datetime) -> list[Run]:
        """Select terminal expired exports while preserving the latest successful run."""
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT id FROM runs WHERE task_id = :task_id AND state = :state AND target_id = :target_id ORDER BY finished_at DESC FETCH NEXT :row_limit ROWS ONLY"),
                {
                    "task_id": task.id,
                    "state": "succeeded",
                    "target_id": task.target_id,
                    "row_limit": 1,
                },
            )
            latest_rows = cursor.fetchall()
        latest = latest_rows[0][0] if latest_rows else None
        state_parameters = {f"state_{index}": value for index, value in enumerate(sorted(TERMINAL))}
        state_placeholders = ", ".join(":" + name for name in state_parameters)
        with self.db.cursor() as cursor:
            cursor.execute(
                (
                    f"SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE task_id = :task_id AND state IN ({state_placeholders}) AND finished_at < :cutoff AND (:latest IS NULL OR id <> :latest) AND target_id = :target_id"
                ),
                {
                    "task_id": task.id,
                    "cutoff": cutoff,
                    "latest": latest,
                    "target_id": task.target_id,
                }
                | state_parameters,
            )
            query_rows = [
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
        return query_rows

    # Credential cleanup

    def remove_unused_credentials(self, credential_ids: set[str]) -> None:
        """Delete credentials only when no saved connection or run snapshot uses them.

        Guarantees that credentials embedded in historical run snapshots or attached
        to existing targets/registrations are never orphaned or deleted prematurely.

        Args:
            credential_ids (set[str]): Candidate set of credential primary key identifiers.

        Returns:
            None: Deletes only unreferenced credentials from the database.
        """
        with self.db.cursor() as cursor:
            cursor.execute("SELECT snapshot FROM runs", {})
            snapshots = [tuple(value.read() if hasattr(value, "read") else value for value in row) for row in cursor.fetchall()]
        snapshot_credentials = {json.loads(s=row[0]).get("credential_id") for row in snapshots}
        for credential_id in credential_ids:
            if credential_id in snapshot_credentials:
                continue
            with self.db.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM registrations WHERE credential_id = :credential_id",
                    {"credential_id": credential_id},
                )
                registrations = cursor.fetchall()
            with self.db.cursor() as cursor:
                cursor.execute(
                    ("SELECT id FROM targets WHERE admin_credential_id = :credential_id OR export_credential_id = :credential_id"),
                    {"credential_id": credential_id},
                )
                targets = cursor.fetchall()
            if not registrations and not targets:
                with self.db.cursor() as cursor:
                    parameters = {"id": credential_id}
                    cursor.execute(
                        ("DELETE FROM credentials WHERE id = :id"),
                        parameters,
                    )

    # Notification deliveries

    def deliveries(self, page: int) -> tuple[list[Delivery], int]:
        """Read delivery history and its total count."""
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries ORDER BY next_attempt_at DESC OFFSET :row_offset ROWS FETCH NEXT :row_limit ROWS ONLY"),
                {"row_offset": (page - 1) * 25, "row_limit": 25},
            )
            items = [Delivery(id=row[0], channel=row[1], payload=row[2].read() if hasattr(row[2], "read") else row[2], state=row[3], attempts=row[4], next_attempt_at=row[5], error=row[6].read() if hasattr(row[6], "read") else row[6]) for row in cursor.fetchall()]
        with self.db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM notification_deliveries", {})
            query_rows = cursor.fetchall()
        return items, query_rows[0][0]

    def due_deliveries(self, current: datetime, limit: int) -> list[Delivery]:
        """Read the next bounded batch of pending notification attempts."""
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT id, channel, payload, state, attempts, next_attempt_at, error FROM notification_deliveries WHERE state = :state AND next_attempt_at <= :current ORDER BY next_attempt_at FETCH NEXT :row_limit ROWS ONLY"),
                {"state": "pending", "current": current, "row_limit": limit},
            )
            query_rows = [Delivery(id=row[0], channel=row[1], payload=row[2].read() if hasattr(row[2], "read") else row[2], state=row[3], attempts=row[4], next_attempt_at=row[5], error=row[6].read() if hasattr(row[6], "read") else row[6]) for row in cursor.fetchall()]
        return query_rows

    # Audit history

    def audit(self, page: int) -> list[AuditEvent]:
        """Read one page of recent audit records.

        Args:
            page (int): One-based page index for pagination (page size of 50).

        Returns:
            list[AuditEvent]: Recent audit records ordered by created_at DESC.
        """
        with self.db.cursor() as cursor:
            cursor.execute(
                ("SELECT id, user_id, action, resource_id, created_at FROM audit_events ORDER BY created_at DESC OFFSET :row_offset ROWS FETCH NEXT :row_limit ROWS ONLY"),
                {"row_offset": (page - 1) * 50, "row_limit": 50},
            )
            query_rows = [AuditEvent(id=row[0], user_id=row[1], action=row[2], resource_id=row[3], created_at=row[4]) for row in cursor.fetchall()]
        return query_rows

    def record_audit(self, user: User, action: str, resource_id: str) -> None:
        """Stage an audit entry atomically with the workflow's changes."""
        audit = AuditEvent(user_id=user.id, action=action, resource_id=resource_id)
        with self.db.cursor() as cursor:
            parameters = {
                "id": audit.id,
                "user_id": audit.user_id,
                "action": audit.action,
                "resource_id": audit.resource_id,
                "created_at": audit.created_at,
            }
            cursor.execute(
                ("INSERT INTO audit_events (id, user_id, action, resource_id, created_at) VALUES (:id, :user_id, :action, :resource_id, :created_at)"),
                parameters,
            )
