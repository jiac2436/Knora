from __future__ import annotations

from app import account


def test_registration_login_logout_and_reset(client, monkeypatch):
    sent = []
    monkeypatch.setattr(account, "_mail_ready", lambda: None)
    monkeypatch.setattr(account, "send_account_mail", lambda address, subject, body: sent.append((address, subject, body)))

    registration = client.post("/v1/auth/register", json={
        "email": "Alice@Example.com", "name": "Alice", "password": "correct horse battery staple"})
    assert registration.status_code == 202
    assert len(sent) == 1
    assert client.post("/v1/auth/sessions", json={
        "email": "alice@example.com", "password": "correct horse battery staple"}).status_code == 401

    code = sent[-1][2].splitlines()[2]
    verified = client.post("/v1/auth/verify", json={"token": code})
    assert verified.status_code == 200, verified.text
    assert verified.json()["user"]["display_name"] == "Alice"
    assert client.post("/v1/auth/verify", json={"token": code}).status_code == 400

    login = client.post("/v1/auth/sessions", json={
        "email": "alice@example.com", "password": "correct horse battery staple"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/me", headers=headers).status_code == 200
    other_session = client.post("/v1/auth/sessions", json={
        "email": "alice@example.com", "password": "correct horse battery staple"}).json()["access_token"]
    assert client.delete("/v1/auth/sessions/current", headers=headers).status_code == 204
    assert client.get("/v1/me", headers=headers).status_code == 401

    unknown = client.post("/v1/auth/password-reset", json={"email": "nobody@example.com"})
    requested = client.post("/v1/auth/password-reset", json={"email": "alice@example.com"})
    assert unknown.status_code == requested.status_code == 202
    assert unknown.json() == requested.json()
    reset_code = sent[-1][2].splitlines()[2]
    changed = client.post("/v1/auth/password-reset/confirm", json={
        "token": reset_code, "password": "new correct horse battery staple"})
    assert changed.status_code == 200
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {other_session}"}).status_code == 401
    assert client.post("/v1/auth/password-reset/confirm", json={
        "token": reset_code, "password": "another correct horse battery staple"}).status_code == 400
    assert client.post("/v1/auth/sessions", json={
        "email": "alice@example.com", "password": "correct horse battery staple"}).status_code == 401
    assert client.post("/v1/auth/sessions", json={
        "email": "alice@example.com", "password": "new correct horse battery staple"}).status_code == 200


def test_password_login_locks_after_repeated_failure(client, monkeypatch):
    sent = []
    monkeypatch.setattr(account, "_mail_ready", lambda: None)
    monkeypatch.setattr(account, "send_account_mail", lambda address, subject, body: sent.append(body))
    client.post("/v1/auth/register", json={
        "email": "lock@example.com", "name": "Lock", "password": "correct horse battery staple"})
    client.post("/v1/auth/verify", json={"token": sent[0].splitlines()[2]})
    for _ in range(5):
        assert client.post("/v1/auth/sessions", json={
            "email": "lock@example.com", "password": "wrong-password"}).status_code == 401
    assert client.post("/v1/auth/sessions", json={
        "email": "lock@example.com", "password": "correct horse battery staple"}).status_code == 429


def test_existing_development_user_can_claim_email_after_verification(client, monkeypatch):
    sent = []
    monkeypatch.setattr(account, "_mail_ready", lambda: None)
    monkeypatch.setattr(account, "send_account_mail", lambda address, subject, body: sent.append(body))
    existing = client.post("/v1/dev/sessions", json={
        "email": "legacy@example.com", "name": "Legacy"}).json()
    response = client.post("/v1/auth/register", json={
        "email": "legacy@example.com", "name": "Different",
        "password": "correct horse battery staple"})
    assert response.status_code == 202
    client.post("/v1/auth/verify", json={"token": sent[0].splitlines()[2]})
    login = client.post("/v1/auth/sessions", json={
        "email": "legacy@example.com", "password": "correct horse battery staple"})
    assert login.status_code == 200
    assert login.json()["user"]["id"] == existing["user"]["id"]
