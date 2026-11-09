from datetime import timedelta

from app.database import SessionLocal
from app.models import AgentConnection, utcnow
from test_m1_api import create_agent


def test_list_is_owner_scoped_without_credentials(client, account):
    agent = create_agent(client, account)
    response = client.get("/v1/agent-connections", headers=account["headers"])
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == agent["id"] and item["domain_ids"] == [account["domain"]["id"]]
    assert item["status"] == "active"
    assert "access_token" not in item and "token_hash" not in item
    assert agent["access_token"] not in response.text
    other = client.post("/v1/dev/sessions", json={"email": "other-agent@example.test", "name": "Other"}).json()
    assert client.get("/v1/agent-connections", headers={"Authorization": "Bearer " + other["access_token"]}).json() == {"items": []}
    assert client.get("/v1/agent-connections", headers={"Authorization": "Bearer " + agent["access_token"]}).status_code == 401


def test_list_effective_expiry_and_revocation(client, account):
    first, second = create_agent(client, account), create_agent(client, account)
    with SessionLocal() as db:
        db.get(AgentConnection, first["id"]).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.delete("/v1/agent-connections/" + second["id"], headers=account["headers"]).status_code == 204
    items = {item["id"]: item for item in client.get("/v1/agent-connections", headers=account["headers"]).json()["items"]}
    assert items[first["id"]]["status"] == "expired"
    assert items[second["id"]]["status"] == "revoked" and items[second["id"]]["revoked_at"]
    for agent in (first, second):
        assert client.post("/v1/mcp/tools/search_knowledge", headers={"Authorization": "Bearer " + agent["access_token"]},
                           json={"query": "测试"}).status_code == 401


def managed(client, account, name='Codex', domains=None):
    response = client.post('/v1/agent-connections', headers=account['headers'], json={
        'client_name': name, 'domain_ids': domains or [account['domain']['id']]})
    assert response.status_code == 201, response.text
    return response.json()


def agent_search(client, token):
    return client.post('/v1/mcp/tools/search_knowledge', headers={'Authorization': 'Bearer ' + token},
                       json={'query': '测试'})


def test_managed_client_keeps_only_one_effective_grant_and_other_clients_work(client, account):
    first = managed(client, account)
    claude = managed(client, account, 'Claude Code')
    second = managed(client, account)
    assert agent_search(client, first['access_token']).status_code == 401
    assert agent_search(client, second['access_token']).status_code == 200
    assert agent_search(client, claude['access_token']).status_code == 200
    items = client.get('/v1/agent-connections', headers=account['headers']).json()['items']
    assert [row['id'] for row in items if row['client_name'] == 'Codex' and row['status'] == 'active'] == [second['id']]
    assert next(row for row in items if row['id'] == first['id'])['status'] == 'revoked'


def test_update_scope_preserves_token_identity_expiry_and_does_not_include_future_domains(client, account):
    first = managed(client, account)
    original_expiry = first['expires_at']
    second_domain = client.post('/v1/domains', headers=account['headers'], json={'name': '第二知识库'}).json()['id']
    response = client.patch('/v1/agent-connections/' + first['id'], headers=account['headers'],
                            json={'domain_ids': [second_domain, second_domain]})
    assert response.status_code == 204
    client.post('/v1/domains', headers=account['headers'], json={'name': '以后新建知识库'})
    item = client.get('/v1/agent-connections', headers=account['headers']).json()['items'][0]
    assert item['domain_ids'] == [second_domain] and item['id'] == first['id'] and item['expires_at'] == original_expiry
    assert agent_search(client, first['access_token']).status_code == 200
    assert 'access_token' not in item


def test_scope_update_validates_before_modifying_and_rejects_agent_or_other_owner(client, account):
    first = managed(client, account)
    path = '/v1/agent-connections/' + first['id']
    assert client.patch(path, headers=account['headers'], json={'domain_ids': []}).status_code == 422
    assert client.patch(path, headers=account['headers'], json={'domain_ids': ['missing']}).status_code == 404
    assert client.patch(path, headers={'Authorization': 'Bearer ' + first['access_token']},
                        json={'domain_ids': [account['domain']['id']]}).status_code == 401
    other = client.post('/v1/dev/sessions', json={'email': 'scope-other@example.test', 'name': 'Other'}).json()
    assert client.patch(path, headers={'Authorization': 'Bearer ' + other['access_token']},
                        json={'domain_ids': [account['domain']['id']]}).status_code == 404
    assert client.get('/v1/agent-connections', headers=account['headers']).json()['items'][0]['domain_ids'] == [account['domain']['id']]


def test_legacy_duplicates_are_ineffective_and_do_not_revive_when_latest_revoked_or_expired(client, account):
    first, second = managed(client, account), managed(client, account)
    with SessionLocal() as db:
        old = db.get(AgentConnection, first['id'])
        old.status, old.revoked_at = 'active', None  # Simulate a pre-update database.
        db.commit()
    assert agent_search(client, first['access_token']).status_code == 401
    rows = client.get('/v1/agent-connections', headers=account['headers']).json()['items']
    assert next(row for row in rows if row['id'] == first['id'])['status'] == 'superseded'
    assert client.patch('/v1/agent-connections/' + first['id'], headers=account['headers'],
                        json={'domain_ids': [account['domain']['id']]}).status_code == 409
    with SessionLocal() as db:
        db.get(AgentConnection, second['id']).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert agent_search(client, first['access_token']).status_code == 401
    assert agent_search(client, second['access_token']).status_code == 401
    client.delete('/v1/agent-connections/' + second['id'], headers=account['headers'])
    assert agent_search(client, first['access_token']).status_code == 401


def test_scope_update_collapses_legacy_rows_without_touching_claude(client, account):
    first, current = managed(client, account), managed(client, account)
    claude = managed(client, account, 'Claude Code')
    with SessionLocal() as db:
        old = db.get(AgentConnection, first['id'])
        old.status, old.revoked_at = 'active', None
        db.commit()
    assert client.patch('/v1/agent-connections/' + current['id'], headers=account['headers'],
                        json={'domain_ids': [account['domain']['id']]}).status_code == 204
    with SessionLocal() as db:
        assert db.get(AgentConnection, first['id']).revoked_at is not None
    assert agent_search(client, claude['access_token']).status_code == 200


def test_current_client_is_returned_even_after_more_than_100_history_rows(client, account):
    current = managed(client, account, 'Claude Code')
    with SessionLocal() as db:
        owner = db.get(AgentConnection, current['id']).owner_id
        for index in range(105):
            db.add(AgentConnection(owner_id=owner, client_name='Legacy API Agent', token_hash=f'{index:064x}',
                expires_at=utcnow() + timedelta(days=1), created_at=utcnow() + timedelta(seconds=index + 1)))
        db.commit()
    rows = client.get('/v1/agent-connections', headers=account['headers']).json()['items']
    assert any(row['id'] == current['id'] and row['status'] == 'active' for row in rows)
    assert len(rows) <= 103
