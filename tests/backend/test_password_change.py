"""Self-service password changes and session revocation."""

import pytest
from conftest import login


@pytest.mark.parametrize(argnames="role", argvalues=["admin", "operator", "viewer"])
def test_change_password(client, db, role):
    """All roles can change their own password while revoking only their other sessions."""
    login(client=client, role="admin")
    admin_token = client.cookies.get(name="oraexport_session")
    profile = login(client=client, role=role).json()
    old_token = client.cookies.get(name="oraexport_session")
    login(client=client, role=role)
    current_token = client.cookies.get(name="oraexport_session")
    response = client.post(
        url="/api/v1/auth/password",
        json={"current_password": "a-strong-test-password", "new_password": "replacement-password"},
    )
    assert response.status_code == 200
    assert client.get(url="/api/v1/auth/me").status_code == 200
    with db.cursor() as cursor:
        cursor.execute(statement="SELECT password_hash FROM app_users WHERE username = :username", parameters={"username": role})
        password_hash = cursor.fetchall()[0][0]
        cursor.execute(statement="SELECT action FROM audit_events WHERE resource_id = :resource_id", parameters={"resource_id": profile["id"]})
        assert ("password_changed",) in cursor.fetchall()
    assert password_hash != "replacement-password"
    client.cookies.clear()
    assert client.get(url="/api/v1/auth/me", headers={"Cookie": f"oraexport_session={old_token}"}).status_code == 401
    assert client.get(url="/api/v1/auth/me", headers={"Cookie": f"oraexport_session={current_token}"}).status_code == 200
    if role != "admin":
        assert client.get(url="/api/v1/auth/me", headers={"Cookie": f"oraexport_session={admin_token}"}).status_code == 200
    assert login(client=client, role=role).status_code == 401
    assert client.post(url="/api/v1/auth/login", json={"username": role, "password": "replacement-password"}).status_code == 200


@pytest.mark.parametrize(
    argnames=("body", "status"),
    argvalues=[
        ({"current_password": "wrong-password", "new_password": "replacement-password"}, 400),
        ({"current_password": "a-strong-test-password", "new_password": "short"}, 422),
        ({"current_password": "a-strong-test-password", "new_password": "a-strong-test-password"}, 400),
        ({"new_password": "replacement-password"}, 422),
        ({"current_password": "a-strong-test-password", "new_password": "replacement-password", "user_id": "admin"}, 422),
    ],
)
def test_rejected_password_change(client, body, status):
    """Invalid credentials and payloads preserve the original password and session."""
    login(client=client, role="viewer")
    response = client.post(url="/api/v1/auth/password", json=body)
    assert response.status_code == status
    assert "replacement-password" not in response.text
    assert "a-strong-test-password" not in response.text
    assert client.get(url="/api/v1/auth/me").status_code == 200
    assert login(client=client, role="viewer").status_code == 200


def test_password_change_requires_session(client):
    """Unauthenticated callers cannot change passwords."""
    response = client.post(url="/api/v1/auth/password", json={"current_password": "old-password", "new_password": "replacement-password"})
    assert response.status_code == 401
