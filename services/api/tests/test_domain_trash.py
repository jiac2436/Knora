from dataclasses import replace

import pytest
from sqlalchemy import select

from app import services, local_trash
from app.database import SessionLocal
from app.models import Source, DomainTrash
from conftest import capture_payload


@pytest.fixture()
def local(client, account, monkeypatch, tmp_path):
    for module in (services, local_trash):
        monkeypatch.setattr(module, 'settings', replace(module.settings, environment='local', database_url=f'sqlite:///{tmp_path / "knowledge.db"}', local_data_dir=str(tmp_path), local_cache_dir=str(tmp_path / 'cache'), local_log_dir=str(tmp_path / 'logs')))
    return client, account


def capture(client, account, text):
    body = capture_payload(account['domain']['id'])
    body['content']['text'] = text
    body['source_url'] = 'https://example.com/' + text
    response = client.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': text}, json=body)
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_delete_restore_preserves_prior_trash_and_blocks_capture(local):
    client, a = local
    first = capture(client, a, 'first')
    previous = capture(client, a, 'previous')
    client.delete('/v1/sources/' + previous, headers=a['headers'])
    path = '/v1/domains/' + a['domain']['id']
    result = client.delete(path, headers=a['headers'])
    assert result.status_code == 200, result.text
    assert result.json()['source_count'] == 1
    assert client.get('/v1/domains', headers=a['headers']).json()['items'] == []
    assert len(client.get('/v1/domains/trash', headers=a['headers']).json()['items']) == 1
    assert client.post('/v1/trash/' + first + '/restore', headers=a['headers']).status_code == 409
    assert client.post('/v1/captures', headers={**a['headers'], 'Idempotency-Key': 'after-delete'},
                       json=capture_payload(a['domain']['id'])).status_code == 404
    assert client.post(path + '/restore', headers=a['headers']).status_code == 200
    with SessionLocal() as db:
        assert db.get(Source, first).deleted_at is None
        assert db.get(Source, previous).deleted_at is not None
    assert len(client.get('/v1/domains', headers=a['headers']).json()['items']) == 1


def test_empty_domain_repeat_and_owner_isolation(local):
    client, a = local
    token = client.post('/v1/dev/sessions', json={'email':'other-domain@example.com', 'name':'Other'}).json()['access_token']
    path = '/v1/domains/' + a['domain']['id']
    assert client.delete(path, headers={'Authorization': 'Bearer ' + token}).status_code == 404
    assert client.delete(path).status_code == 401
    assert client.delete(path, headers=a['headers']).status_code == 200
    assert client.delete(path, headers=a['headers']).status_code == 404
    assert client.post(path + '/restore', headers=a['headers']).status_code == 200


def test_delete_is_atomic(local, monkeypatch):
    from app import domain_trash
    client, a = local
    first = capture(client, a, 'atomic1')
    second = capture(client, a, 'atomic2')
    original = domain_trash.soft_delete_source
    count = 0
    def fail(db, owner, identifier, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            from app.errors import ApiError
            raise ApiError(409, 'test_failure', 'test')
        return original(db, owner, identifier, **kwargs)
    monkeypatch.setattr(domain_trash, 'soft_delete_source', fail)
    assert client.delete('/v1/domains/' + a['domain']['id'], headers=a['headers']).status_code == 409
    with SessionLocal() as db:
        assert db.get(Source, first).deleted_at is None
        assert db.get(Source, second).deleted_at is None
        assert db.get(DomainTrash, a['domain']['id']) is None


def test_upgrade_v15_preserves_domain_and_adds_trash(tmp_path):
    from app import migrations
    from app.database import make_engine
    from app.local_changes import install_change_triggers
    engine = make_engine(f'sqlite:///{tmp_path / "knowledge.db"}')
    migrations.migrate(engine, tmp_path / 'backups')
    with engine.begin() as conn:
        conn.exec_driver_sql('DROP TABLE domain_trash')
        install_change_triggers(conn, version=15)
        conn.exec_driver_sql('DELETE FROM schema_migrations WHERE version = 16')
    migrations.migrate(engine, tmp_path / 'backups')
    with engine.connect() as conn:
        migrations.require_schema(conn)
        assert migrations.schema_version(conn) == 16
    engine.dispose()


def test_restore_failure_rolls_back_entire_domain(local, monkeypatch):
    client, a = local
    first = capture(client, a, 'restore1')
    second = capture(client, a, 'restore2')
    path = '/v1/domains/' + a['domain']['id']
    assert client.delete(path, headers=a['headers']).status_code == 200
    original = local_trash.verify_originals
    count = 0
    def verify(db, source):
        nonlocal count
        count += 1
        if count == 2:
            from app.errors import ApiError
            raise ApiError(409, 'test_original_missing', 'test')
        original(db, source)
    monkeypatch.setattr(local_trash, 'verify_originals', verify)
    assert client.post(path + '/restore', headers=a['headers']).status_code == 409
    with SessionLocal() as db:
        assert db.get(DomainTrash, a['domain']['id']) is not None
        assert db.get(Source, first).deleted_at is not None
        assert db.get(Source, second).deleted_at is not None
