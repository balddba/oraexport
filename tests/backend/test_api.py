"""API authentication, permissions, validation, and reporting tests."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from conftest import login
from dbapi import execute, fetch_records
from oracle_fakes import remote_connection

import oraexport.bootstrap as bootstrap_module
from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.models.audit_event import AuditEvent
from oraexport.models.credential import Credential
from oraexport.models.discovered_pdb import DiscoveredPdb
from oraexport.models.discovery_response import DiscoveryResponse
from oraexport.models.registration import Registration
from oraexport.models.run import Run
from oraexport.models.target import Target
from oraexport.models.target_catalog_response import TargetCatalogResponse
from oraexport.oracle_database import OracleDatabase


def test_edit_registration_preserves_credentials(client, db):
    """Name edits retain discovery and secrets; DSN edits invalidate discovery.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
    """
    login(client)
    # Decompose registration creation into single lines
    orig_payload = {"name": "Original", "dsn": "db/service", "username": "system"}
    orig_payload["password"] = "secret"
    created = client.post("/api/v1/registrations", json=orig_payload).json()
    registration = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": created["id"]},
            )
        ),
        None,
    )
    assert registration is not None
    credential_id = registration.credential_id
    # Decompose multi-variable assignment into single lines
    registration.kind = "cdb"
    execute(
        db,
        ("UPDATE registrations SET kind = :kind WHERE id = :record_key"),
        {"kind": registration.kind, "record_key": registration.id},
    )
    registration.version = "19c"
    execute(
        db,
        ("UPDATE registrations SET version = :version WHERE id = :record_key"),
        {"version": registration.version, "record_key": registration.id},
    )
    db.commit()
    endpoint = f"/api/v1/registrations/{registration.id}"
    renamed = client.put(endpoint, json={"name": "Renamed", "dsn": "db/service"})
    assert renamed.status_code == 200
    assert renamed.json() == created | {"name": "Renamed", "kind": "cdb", "version": "19c"}
    # Changing DSN invalidates previous discovery state to 'unknown'
    changed = client.put(endpoint, json={"name": "Renamed", "dsn": "new/service"})
    assert changed.status_code == 200
    assert changed.json()["kind"] == "unknown"
    assert changed.json()["version"] is None
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Registration,
                    ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                    {"record_key": registration.id},
                )
            ),
        ).credential_id
        == credential_id
    )
    assert (
        EncryptionSerivce.decrypt(
            next(
                iter(
                    fetch_records(
                        db,
                        Credential,
                        ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                        {"record_key": credential_id},
                    )
                ),
            ).ciphertext
        )
        == "secret"
    )
    assert next(
        iter(
            fetch_records(
                db,
                AuditEvent,
                ("SELECT id, user_id, action, resource_id, created_at FROM audit_events WHERE action = :action FETCH NEXT :row_limit ROWS ONLY"),
                dict({"action": "database_updated"}, row_limit=1),
            )
        ),
        None,
    )
    missing_ep = "/api/v1/registrations/missing"
    assert client.put(missing_ep, json={"name": "Name", "dsn": "db/service"}).status_code == 404


def test_edit_registration_rotates_inherited_credentials(client, db, task):
    """Rotate inherited DBA secrets while preserving exports, private secrets, and snapshots.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
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
    registration = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": target.registration_id},
            )
        ),
        None,
    )
    assert registration is not None
    previous_id = registration.credential_id
    run = OracleDatabase(task.target_id, db).enqueue(task)
    private = Credential(username="private", ciphertext="private-encrypted")
    execute(
        db,
        ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
        private.model_dump(mode="python"),
        clobs=("ciphertext",),
    )

    independent = Target(name="Independent", registration_id=registration.id, dsn="db/private", admin_credential_id=private.id)
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
        independent.model_dump(mode="python"),
    )
    db.commit()
    snapshot = run.snapshot
    login(client)
    # Decompose edit payload into single lines
    edit_payload: dict[str, object] = {"name": "Edited", "dsn": "db/new", "username": "SYS"}
    edit_payload["password"] = "replacement-secret"
    edit_payload["sysdba"] = True
    response = client.put(f"/api/v1/registrations/{registration.id}", json=edit_payload)
    assert response.status_code == 200
    assert "replacement-secret" not in response.text
    assert "ciphertext" not in response.text
    db.rollback()
    replacement_id = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": registration.id},
            )
        ),
    ).credential_id
    assert replacement_id != previous_id
    credential = next(
        iter(
            fetch_records(
                db,
                Credential,
                ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                {"record_key": replacement_id},
            )
        ),
        None,
    )
    assert credential is not None
    assert credential.username == "SYS"
    assert credential.sysdba
    assert EncryptionSerivce.decrypt(credential.ciphertext) == "replacement-secret"
    updated = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": target.id},
            )
        ),
        None,
    )
    assert updated is not None
    assert updated.admin_credential_id == replacement_id
    assert updated.dsn == target.dsn
    assert updated.export_credential_id == previous_id
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Target,
                    ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                    {"record_key": independent.id},
                )
            ),
        ).admin_credential_id
        == private.id
    )
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": run.id},
                )
            ),
        ).snapshot
        == snapshot
    )
    assert (
        EncryptionSerivce.decrypt(
            next(
                iter(
                    fetch_records(
                        db,
                        Credential,
                        ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                        {"record_key": previous_id},
                    )
                ),
            ).ciphertext
        )
        == "db-password"
    )


def test_edit_registration_removes_unused_old_credential(client, db):
    """A replacement discards the previous secret only when it has no remaining users.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
    """
    login(client)
    orig_payload = {"name": "Original", "dsn": "db/service", "username": "system"}
    orig_payload["password"] = "secret"
    created = client.post("/api/v1/registrations", json=orig_payload).json()
    previous_id = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": created["id"]},
            )
        ),
    ).credential_id
    # Decompose update payload into single lines
    update_payload: dict[str, object] = {"name": "Original", "dsn": "db/service", "username": "system"}
    update_payload["password"] = "new-secret"
    update_payload["sysdba"] = False
    response = client.put(f"/api/v1/registrations/{created['id']}", json=update_payload)
    assert response.status_code == 200
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Credential,
                    ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                    {"record_key": previous_id},
                )
            ),
            None,
        )
        is None
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"username": "system"},
        {"password": "SECRET-INPUT"},
        {"sysdba": True},
        {"username": "system", "password": "", "sysdba": False},
        {"username": None, "password": None, "sysdba": None},
        {"dsn": "user/SECRET-INPUT@db/service"},
        {"oops": "SECRET-INPUT"},
        {"name": ""},
    ],
)
def test_edit_registration_rejects_invalid_input(client, db, task, extra):
    """Reject incomplete credentials and unsafe inputs without echoing or mutating them.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        extra (dict[str, Any]): Invalid dictionary extra attributes.
    """
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
    registration = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": target.registration_id},
            )
        ),
        None,
    )
    assert registration is not None
    previous_id = registration.credential_id
    login(client)
    put_payload = {"name": "Edited", "dsn": "db/new"} | extra
    response = client.put(f"/api/v1/registrations/{registration.id}", json=put_payload)
    assert response.status_code == 422
    assert "SECRET-INPUT" not in response.text
    db.rollback()
    stored = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": registration.id},
            )
        ),
        None,
    )
    assert stored is not None
    assert stored.name == registration.name
    assert stored.dsn == registration.dsn
    assert stored.credential_id == previous_id


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_edit_registration_requires_admin(client, db, task, role):
    """Editing connections follows the existing administrator permissions.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        role (str): Non-administrator user role ('viewer' or 'operator').
    """
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
    login(client, role)
    ep = f"/api/v1/registrations/{target.registration_id}"
    assert client.put(ep, json={"name": "Edited", "dsn": "db/service"}).status_code == 403


def test_discovery_suggests_target_connections(client, db, task, monkeypatch):
    """Discovery returns service DSNs built from the registered listener.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """

    @contextmanager
    def fake_connect(dsn, credential):
        """Yield a discovered CDB without making network calls.

        Args:
            dsn (str): Target connection string.
            credential (Credential): Target database credentials.

        Yields:
            RemoteConnection: Recording driver session double.
        """

        class FakeOracle:
            """Return network names reported by Oracle."""

            def discover(self):
                """Describe a PDB and its services.

                Returns:
                    DiscoveryResponse: Simulated discovery response with PDB services.
                """
                return DiscoveryResponse(
                    kind="cdb",
                    version="19c",
                    pdbs=[DiscoveredPdb(name="FINANCE", open_mode="READ WRITE", services=["finance.example"])],
                )

        yield remote_connection(FakeOracle())

    monkeypatch.setattr(bootstrap_module.OracleUtilities, "connect", fake_connect)
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
    login(client)
    response = client.post(f"/api/v1/registrations/{target.registration_id}/discover")
    assert response.status_code == 200
    service = response.json()["pdbs"][0]["services"][0]
    assert service["name"] == "finance.example"
    assert service["dsn"] == "test:1521/finance.example"


@pytest.mark.parametrize(("container", "status"), [("FINANCE", 201), ("OTHER", 400)])
def test_add_discovered_pdb_verifies_container(client, db, task, monkeypatch, container, status):
    """Verify the selected PDB identity before persisting a suggested target.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
        container (str): Connected container name reported by catalog.
        status (int): Expected HTTP response status code.
    """

    @contextmanager
    def fake_connect(dsn, credential):
        """Yield a catalog for the service being verified.

        Args:
            dsn (str): Connection string.
            credential (Credential): Authentication credentials.

        Yields:
            RemoteConnection: Recording driver session double.
        """

        class FakeOracle:
            """Return the actual connected container."""

            def catalog(self):
                """Return minimal target metadata.

                Returns:
                    TargetCatalogResponse: Container name and edition metadata.
                """
                return TargetCatalogResponse(kind="pdb", container_name=container, version="19c", edition="Enterprise")

        yield remote_connection(FakeOracle())

    monkeypatch.setattr(bootstrap_module.OracleUtilities, "connect", fake_connect)
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
    login(client)
    tgt_payload = {"registration_id": target.registration_id, "name": "Discovered Finance"}
    tgt_payload["dsn"] = "test:1521/finance.example"
    tgt_payload["pdb_name"] = "FINANCE"
    response = client.post("/api/v1/targets", json=tgt_payload)
    assert response.status_code == status
    db.rollback()
    stored = next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE name = :name FETCH NEXT :row_limit ROWS ONLY"),
                dict({"name": "Discovered Finance"}, row_limit=1),
            )
        ),
        None,
    )
    if status == 201:
        assert stored is not None
        assert stored.container_name == "FINANCE"
        assert stored.admin_credential_id == target.admin_credential_id
    else:
        assert "different PDB" in response.json()["detail"]
        assert stored is None


def test_remove_database_registration(client, db):
    """Remove unused registrations and credentials while recording an audit entry.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
    """
    login(client)
    reg_payload = {"name": "Unused", "dsn": "db/service"}
    reg_payload["username"] = "system"
    reg_payload["password"] = "secret"
    response = client.post("/api/v1/registrations", json=reg_payload)
    registration = response.json()
    registration_id = registration["id"]
    credential_id = next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": registration_id},
            )
        ),
    ).credential_id
    response = client.delete(f"/api/v1/registrations/{registration_id}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Registration,
                    ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                    {"record_key": registration_id},
                )
            ),
            None,
        )
        is None
    )
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Credential,
                    ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                    {"record_key": credential_id},
                )
            ),
            None,
        )
        is None
    )
    audit_filter = "action = :action AND resource_id = :resource_id"
    audit_params = {"action": "database_removed", "resource_id": registration_id}
    assert next(
        iter(
            fetch_records(
                db,
                AuditEvent,
                ("SELECT id, user_id, action, resource_id, created_at FROM audit_events") + (" WHERE " + audit_filter if audit_filter else "") + " FETCH NEXT :row_limit ROWS ONLY",
                dict(audit_params or {}, row_limit=1),
            )
        ),
        None,
    )
    assert client.delete(f"/api/v1/registrations/{registration_id}").status_code == 404


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_remove_connection_requires_admin(client, db, task, role):
    """Verify nonadministrators cannot remove either saved connection type.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        role (str): Non-administrator user role under test.
    """
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
    login(client, role)
    assert client.delete(f"/api/v1/registrations/{target.registration_id}").status_code == 403
    assert client.delete(f"/api/v1/targets/{target.id}").status_code == 403


def test_remove_database_preserves_dependencies(client, db, task):
    """Ensure registrations with targets and targets with tasks remain protected.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
    login(client)
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
    assert client.delete(f"/api/v1/registrations/{target.registration_id}").status_code == 409
    assert client.delete(f"/api/v1/targets/{target.id}").status_code == 409
    db.rollback()
    assert next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": target.id},
            )
        ),
        None,
    )
    assert next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": target.registration_id},
            )
        ),
        None,
    )
    assert next(
        iter(
            fetch_records(
                db,
                Credential,
                ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                {"record_key": target.admin_credential_id},
            )
        ),
        None,
    )


def test_remove_unused_target_preserves_shared_credential(client, db, task):
    """Deleting an unused target retains inherited credentials and its registration.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
    original = next(
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
    assert original is not None
    export = Credential(username="EXPORT", ciphertext="encrypted")
    execute(
        db,
        ("INSERT INTO credentials (id, username, ciphertext, sysdba) VALUES (:id, :username, :ciphertext, :sysdba)"),
        export.model_dump(mode="python"),
        clobs=("ciphertext",),
    )

    unused = Target(name="Unused", registration_id=original.registration_id, dsn="db/other", admin_credential_id=original.admin_credential_id, export_credential_id=export.id)
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
        unused.model_dump(mode="python"),
    )
    db.commit()
    unused_id = unused.id
    export_id = export.id
    login(client)
    assert client.delete(f"/api/v1/targets/{unused_id}").status_code == 200
    db.rollback()
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Target,
                    ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                    {"record_key": unused_id},
                )
            ),
            None,
        )
        is None
    )
    assert (
        next(
            iter(
                fetch_records(
                    db,
                    Credential,
                    ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                    {"record_key": export_id},
                )
            ),
            None,
        )
        is None
    )
    assert next(
        iter(
            fetch_records(
                db,
                Credential,
                ("SELECT id, username, ciphertext, sysdba FROM credentials WHERE id = :record_key"),
                {"record_key": original.admin_credential_id},
            )
        ),
        None,
    )
    assert next(
        iter(
            fetch_records(
                db,
                Registration,
                ("SELECT id, name, dsn, credential_id, kind, version FROM registrations WHERE id = :record_key"),
                {"record_key": original.registration_id},
            )
        ),
        None,
    )
    assert client.delete(f"/api/v1/targets/{unused_id}").status_code == 404


def test_remove_target_preserves_history_after_task_moves(client, db, task):
    """Historical exports keep a target protected even after its policy moves.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
    """
    original = next(
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
    assert original is not None
    run = OracleDatabase(task.target_id, db).enqueue(task)
    run.state = "failed"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    replacement = Target(name="Replacement", registration_id=original.registration_id, dsn="db/other", admin_credential_id=original.admin_credential_id)
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
        replacement.model_dump(mode="python"),
    )

    task.target_id = replacement.id
    execute(
        db,
        ("UPDATE tasks SET target_id = :target_id WHERE id = :record_key"),
        {"target_id": task.target_id, "record_key": task.id},
    )
    db.commit()
    login(client)
    assert client.delete(f"/api/v1/targets/{original.id}").status_code == 409
    db.rollback()
    assert next(
        iter(
            fetch_records(
                db,
                Target,
                ("SELECT id, registration_id, name, dsn, admin_credential_id, export_credential_id, directory, tablespace, provisioned, edition, container_name FROM targets WHERE id = :record_key"),
                {"record_key": original.id},
            )
        ),
        None,
    )


def test_login_logout_and_roles(client):
    """Verify viewer access is read-only and logout invalidates the token.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
    """
    assert client.get("/api/v1/tasks").status_code == 401
    assert login(client, "viewer").status_code == 200
    assert client.get("/api/v1/tasks").status_code == 200
    forbidden_payload = {"name": "DB", "dsn": "host/service"}
    forbidden_payload["username"] = "system"
    forbidden_payload["password"] = "hidden"
    assert client.post("/api/v1/registrations", json=forbidden_payload).status_code == 403
    assert client.get("/api/v1/users").status_code == 403
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_secrets_and_structured_validation(client, db):
    """Registration responses and validation errors do not expose submitted secrets.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
    """
    login(client)
    body = {"name": "Prod", "dsn": "db/service"}
    body["username"] = "system"
    body["password"] = "SUPER-SECRET"
    response = client.post("/api/v1/registrations", json=body)
    assert response.status_code == 201
    assert "SUPER-SECRET" not in response.text
    assert "credential" not in response.text
    credential = next(
        iter(
            fetch_records(
                db,
                Credential,
                "SELECT id, username, ciphertext, sysdba FROM credentials FETCH NEXT :row_limit ROWS ONLY",
                dict({} or {}, row_limit=1),
            )
        ),
        None,
    )
    assert credential is not None
    assert "SUPER-SECRET" not in credential.ciphertext
    assert EncryptionSerivce.decrypt(credential.ciphertext) == "SUPER-SECRET"
    response = client.post("/api/v1/registrations", json=body | {"oops": "SECRET-INPUT"})
    assert response.status_code == 422
    assert "SECRET-INPUT" not in response.text
    assert response.json()["detail"][0]["loc"][-1] == "oops"
    assert client.post("/api/v1/registrations", json=body).status_code == 409


def test_same_origin_and_cookie(client):
    """Reject cross-origin mutation and issue an HTTP-only test cookie.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
    """
    origin_headers = {"Origin": "https://evil.example"}
    evil_auth = {"username": "admin", "password": "a-strong-test-password"}
    response = client.post("/api/v1/auth/login", headers=origin_headers, json=evil_auth)
    assert response.status_code == 403
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie


def test_run_history_and_cancel(client, task):
    """Manual runs expose snapshots, reject duplicates, and support queued cancellation.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        task (Task): Seeded backup task record.
    """
    login(client, "operator")
    response = client.post(f"/api/v1/tasks/{task.id}/run")
    assert response.status_code == 202
    run = response.json()
    assert "dsn" not in run["snapshot"]
    assert "credential_id" not in run["snapshot"]
    assert run["size_bytes"] is None
    assert client.post(f"/api/v1/tasks/{task.id}/run").status_code == 409
    assert client.get("/api/v1/runs?page=0").status_code == 422
    assert client.get("/api/v1/runs?state=made-up").status_code == 400
    assert client.get("/api/v1/runs").json()["total"] == 1
    assert client.post(f"/api/v1/runs/{run['id']}/cancel").json()["state"] == "cancelled"
    report = client.get("/api/v1/reports/export.csv")
    assert report.status_code == 200
    assert "Finance" in report.text


def test_task_create_and_edit(client, task, monkeypatch):
    """Verify targets before saving tasks and preserve existing run snapshots.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """

    class FakeOracle:
        """Accept only test schema verification."""

        def verify_task(self, body):
            """Simulate privilege checks.

            Args:
                body (Any): Task specification payload.
            """
            assert body.schema_name == "FINANCE"

    @contextmanager
    def fake_connect(*args):
        """Inject a validated test target connection.

        Args:
            *args (Any): Variable connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(FakeOracle())

    monkeypatch.setattr(bootstrap_module.OracleUtilities, "connect", fake_connect)
    login(client, "operator")
    body = {"name": "Second", "target_id": task.target_id}
    body["schema_name"] = "FINANCE"
    body["directory"] = "EXPORT_DIR"
    response = client.post("/api/v1/tasks", json=body)
    assert response.status_code == 201
    task_id = response.json()["id"]
    edit_payload = body | {"enabled": False}
    assert client.put(f"/api/v1/tasks/{task_id}", json=edit_payload).json()["enabled"] is False
    preview_payload = {"cron": "0 2 * * *", "timezone": "America/Detroit"}
    preview_resp = client.post("/api/v1/schedules/preview", json=preview_payload).json()
    assert len(preview_resp) == 5


def test_notification_secrets(client):
    """Verify only secret-presence indicators are returned to administrators.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
    """
    login(client)
    body = {"webhook_url": "https://notify.example/event", "webhook_token": "SUPER-SECRET"}
    assert client.put("/api/v1/notifications/settings", json=body).status_code == 200
    response = client.get("/api/v1/notifications/settings")
    assert response.json()["has_webhook_token"]
    assert "SUPER-SECRET" not in response.text
    update_payload: dict[str, object] = {"webhook_url": body["webhook_url"]}
    assert client.put("/api/v1/notifications/settings", json=update_payload).status_code == 200
    assert client.get("/api/v1/notifications/settings").json()["has_webhook_token"]
    assert client.post("/api/v1/notifications/test").status_code == 200
    assert client.get("/api/v1/notifications/deliveries").json()["total"] == 1


def test_reconcile_requires_verified_inactive_target(client, db, task, monkeypatch):
    """Verify admin acknowledgment cannot race an active Oracle job.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    run = OracleDatabase(task.target_id, db).enqueue(task)
    run.state = "reconciliation_required"
    execute(
        db,
        ("UPDATE runs SET state = :state WHERE id = :record_key"),
        {"state": run.state, "record_key": run.id},
    )
    db.commit()
    login(client)

    class Remote:
        """Initially active target for acknowledgment verification."""

        active = True

        def verify_inactive(self, job):
            """Refuse acknowledgment until external inactivity is confirmed.

            Args:
                job (Any): Job record to verify.

            Raises:
                ValueError: If Oracle still reports an active job.
            """
            if self.active:
                raise ValueError("Oracle still reports an active job")

    remote = Remote()

    @contextmanager
    def connection(*args):
        """Yield the controllable target state.

        Args:
            *args (Any): Variable connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(remote)

    monkeypatch.setattr(bootstrap_module.OracleUtilities, "connect", connection)
    body = {"outcome": "failed", "reason": "Investigated stopped Oracle job"}
    assert client.post(f"/api/v1/runs/{run.id}/reconcile", json=body).status_code == 400
    remote.active = False
    assert client.post(f"/api/v1/runs/{run.id}/reconcile", json=body).json()["state"] == "failed"


def test_provision_preview_and_retry_owned_account(client, db, task, monkeypatch):
    """Preview excludes password creation for owned accounts and apply reuses password.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """
    login(client)
    body = {"directory": "EXPORT_DIR"}
    response = client.post(f"/api/v1/targets/{task.target_id}/provision/preview", json=body)
    assert response.status_code == 200
    assert not any("CREATE USER" in sql for sql in response.json()["sql"])

    class Remote:
        """Idempotently provisioned remote account."""

        def __init__(self):
            """Expose the cursor account-existence query."""
            self.cursor = MagicMock()
            self.cursor.fetchone.return_value = (1,)

        def account_exists(self, username):
            """Report the already provisioned dedicated account.

            Args:
                username (str): Account username to check.

            Returns:
                bool: True if account exists.
            """
            return True

        def provision(self, options, password):
            """Ensure an owned account does not reset its saved password.

            Args:
                options (Any): Provisioning options.
                password (str | None): Generated password or None.
            """
            assert password is None

        def verify_directory(self, directory):
            """Accept the fixture filesystem destination.

            Args:
                directory (str): Oracle directory object name.
            """
            assert directory == "EXPORT_DIR"

    @contextmanager
    def connection(*args):
        """Yield an existing owned account.

        Args:
            *args (Any): Variable connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(Remote())

    monkeypatch.setattr(bootstrap_module.OracleUtilities, "connect", connection)
    apply_resp = client.post(f"/api/v1/targets/{task.target_id}/provision/apply", json=body)
    assert apply_resp.status_code == 200
    assert client.get("/api/v1/targets").json()[0]["export_username"] == "ORAEXPORT"


def test_edit_export_scope_preserves_queued_runs(client, db, task, monkeypatch):
    """Save multiple schemas or full exports while keeping queued snapshots immutable.

    Args:
        client (TestClient): Authenticated FastAPI HTTP test client.
        db (Connection): Metadata database session.
        task (Task): Seeded backup task record.
        monkeypatch (pytest.MonkeyPatch): Pytest monkeypatch fixture.
    """

    class FakeOracle:
        """Accept valid target options and simulate missing remote schemas."""

        def verify_task(self, body):
            """Reject an unavailable schema before editing metadata.

            Args:
                body (Any): Task specification payload.

            Raises:
                ValueError: If MISSING schema is requested.
            """
            if "MISSING" in body.schema_names:
                raise ValueError("Application schema MISSING does not exist")

    @contextmanager
    def fake_connect(*args):
        """Yield an isolated export-account verifier.

        Args:
            *args (Any): Variable connection arguments.

        Yields:
            RemoteConnection: Recording driver session double.
        """
        yield remote_connection(FakeOracle())

    monkeypatch.setattr(bootstrap_module.OracleUtilities, "connect", fake_connect)
    login(client, "operator")
    body = {"name": "Multiple", "target_id": task.target_id}
    body["directory"] = "EXPORT_DIR"
    body["schema_names"] = ["HR", "FINANCE"]
    created = client.post("/api/v1/tasks", json=body)
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert created.json()["schema_names"] == ["HR", "FINANCE"]
    queued = client.post(f"/api/v1/tasks/{task_id}/run").json()
    assert queued["snapshot"]["schema_names"] == ["HR", "FINANCE"]
    edit_scope_payload = dict(body)
    edit_scope_payload["export_mode"] = "FULL"
    edit_scope_payload["schema_names"] = []
    edit_scope_payload["name"] = "Full target"
    edited = client.put(f"/api/v1/tasks/{task_id}", json=edit_scope_payload)
    assert edited.status_code == 200
    assert edited.json()["export_mode"] == "FULL"
    assert edited.json()["schema_names"] == []
    db.rollback()
    assert json.loads(
        next(
            iter(
                fetch_records(
                    db,
                    Run,
                    ("SELECT id, task_id, target_id, occurrence, snapshot, state, oracle_job, created_at, started_at, finished_at, progress, errors, messages, cancel_requested, last_seen_at, notification_created, master_cleanup_pending FROM runs WHERE id = :record_key"),
                    {"record_key": queued["id"]},
                )
            ),
        ).snapshot
    )["schema_names"] == ["HR", "FINANCE"]
    assert client.get(f"/api/v1/runs/{queued['id']}").json()["snapshot"]["export_mode"] == "SCHEMA"
    client.post(f"/api/v1/runs/{queued['id']}/cancel")
    full_run = client.post(f"/api/v1/tasks/{task_id}/run").json()
    assert full_run["snapshot"]["export_mode"] == "FULL"
    assert full_run["snapshot"]["schema_names"] == []
    rej_payload = body | {"schema_names": ["MISSING"]}
    assert client.put(f"/api/v1/tasks/{task_id}", json=rej_payload).status_code == 400
    stored = next(item for item in client.get("/api/v1/tasks").json() if item["id"] == task_id)
    assert stored["export_mode"] == "FULL"
    assert stored["schema_names"] == []
    assert "Full export" in client.get("/api/v1/reports/export.csv").text
