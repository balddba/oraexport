"""Authentication lifecycle regressions at the HTTP boundary."""

import pytest
from conftest import login


@pytest.mark.parametrize("username", ["admin", "missing-account"])
def test_invalid_credentials_do_not_issue_a_session(client, username):
    """Reject incorrect credentials without revealing whether an account exists.

    Args:
        client (TestClient): Isolated API client.
        username (str): Existing or unknown account name.
    """
    response = client.post("/api/v1/auth/login", json={"username": username, "password": "wrong-password"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid username or password"}
    assert "oraexport_session" not in response.cookies
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logged_out_session_cannot_be_replayed(client):
    """Invalidate the server-side session even if the browser reuses its old cookie.

    Args:
        client (TestClient): Isolated API client.
    """
    assert login(client).status_code == 200
    token = client.cookies["oraexport_session"]
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    response = client.get("/api/v1/auth/me", headers={"Cookie": f"oraexport_session={token}"})
    assert response.status_code == 401


def test_disabling_account_revokes_existing_access(client):
    """Deny a disabled user's active session and future login attempts.

    Args:
        client (TestClient): Isolated API client.
    """
    account = login(client, "operator").json()
    token = client.cookies["oraexport_session"]
    assert login(client).status_code == 200
    response = client.post(f"/api/v1/users/{account['id']}/disable")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    client.cookies.clear()
    response = client.get("/api/v1/auth/me", headers={"Cookie": f"oraexport_session={token}"})
    assert response.status_code == 401
    assert login(client, "operator").status_code == 401


def test_admin_cannot_disable_own_account(client):
    """Preserve administrative access after rejecting a self-disable request.

    Args:
        client (TestClient): Isolated API client.
    """
    account = login(client).json()
    response = client.post(f"/api/v1/users/{account['id']}/disable")
    assert response.status_code == 400
    assert response.json() == {"detail": "Cannot disable your own account"}
    assert client.get("/api/v1/auth/me").json()["enabled"] is True
