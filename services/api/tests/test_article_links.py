"""Local navigation URLs contain identity, never credentials or content."""
from uuid import uuid4
import base64
import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import article_links
from app.article_links import article_url, browser_article_url, local_library_id
from app.database import SessionLocal
from app.models import EvidenceChunk, Library, LocalInstallation, Source
from test_agent_knowledge import knowledge, add_summary


def install_identity(source_id):
    with SessionLocal() as db:
        source = db.get(Source, source_id)
        library = Library(owner_id=source.owner_id)
        db.add(library); db.flush()
        db.add(LocalInstallation(id=1, owner_id=source.owner_id, library_id=library.id, device_id=str(uuid4())))
        db.commit()
        return library.id


def test_nonlocal_sources_do_not_get_a_fabricated_client_link(client, knowledge):
    source, _, auth = knowledge
    response = client.post('/v1/mcp/tools/get_source', headers=auth, json={'source_id': source['id']})
    assert response.status_code == 200 and response.json()['app_url'] is None


def test_all_agent_read_paths_return_the_same_owned_article_link(client, account, knowledge):
    source, _, auth = knowledge
    library = install_identity(source['id'])
    expected = f"knowledge-inbox://library/{library}/source/{source['id']}"
    add_summary(source['id'])
    assert client.get('/v1/me', headers=account['headers']).json()['library_id'] == library
    for tool in ('get_source', 'get_summary', 'get_tags'):
        response = client.post('/v1/mcp/tools/' + tool, headers=auth, json={'source_id': source['id']})
        assert response.status_code == 200, response.text
        assert response.json()['app_url'] == expected
    with SessionLocal() as db:
        item = db.get(Source, source['id'])
        evidence_id = db.scalar(select(EvidenceChunk.id).where(EvidenceChunk.revision_id == item.current_revision_id))
    evidence = client.post('/v1/mcp/tools/get_evidence', headers=auth, json={'evidence_id': evidence_id}).json()
    assert evidence['app_url'] == expected and evidence['title']
    listing = client.post('/v1/mcp/tools/list_knowledge', headers=auth, json={}).json()
    assert next(i for i in listing['items'] if i['source_id'] == source['id'])['app_url'] == expected
    # Link compatibility belongs to the base client and must not require a semantic extension.
    response = client.post('/v1/mcp/tools/search_knowledge', headers=auth, json={'query': '虚拟列表', 'mode': 'exact'})
    assert response.status_code == 200, response.text
    results = response.json()
    assert results['items'] and all(i['app_url'] == expected for i in results['items'])


def test_link_does_not_grant_access_after_connection_revocation(client, knowledge):
    source, agent, auth = knowledge
    install_identity(source['id'])
    with SessionLocal() as db:
        item = db.get(Source, source['id'])
        assert article_url(db, item)
        assert local_library_id(db, str(uuid4())) is None
    assert client.post('/v1/mcp/tools/get_source', json={'source_id': source['id']}).status_code == 401
    with SessionLocal() as db:
        from app.models import AgentConnection, utcnow
        db.get(AgentConnection, agent['id']).revoked_at = utcnow()
        db.commit()
    assert client.post('/v1/mcp/tools/get_source', headers=auth, json={'source_id': source['id']}).status_code == 401


@pytest.mark.parametrize('value', [None, '', 'https://example.test/article',
    'knowledge-inbox://library/not-an-id/source/not-an-id', 'knowledge-inbox://[bad'])
def test_browser_links_do_not_accept_external_or_malformed_targets(value):
    assert browser_article_url('http://127.0.0.1:8090', value) is None


def test_browser_handoff_is_local_navigation_without_article_access(monkeypatch):
    monkeypatch.setattr(article_links, 'settings', SimpleNamespace(environment='local'))
    test_app = FastAPI()
    test_app.include_router(article_links.router)
    library_id, source_id = uuid4(), uuid4()
    native_url = f'knowledge-inbox://library/{library_id}/source/{source_id}'
    url = browser_article_url('http://127.0.0.1:8090', native_url)
    assert url == f'http://127.0.0.1:8090/open/library/{library_id}/source/{source_id}'
    with TestClient(test_app, base_url='http://127.0.0.1:8090', client=('127.0.0.1', 50000)) as browser:
        response = browser.get(url)
        assert response.status_code == 200
        assert f'href="{native_url}"' in response.text
        assert '在 Knora 中打开' in response.text
        assert 'source_url' not in response.text and 'access_token' not in response.text
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
        script = response.text.split('<script>')[1].split('</script>')[0]
        encoded = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{encoded}'" in response.headers['Content-Security-Policy']
        assert browser.get(url, headers={'Host': 'attacker.test'}).status_code == 404
        assert browser.get(url + '?redirect=https://example.test').status_code == 404
        assert browser.get(url.replace(str(source_id), 'invalid')).status_code == 422
        monkeypatch.setattr(article_links, 'settings', SimpleNamespace(environment='development'))
        assert browser.get(url).status_code == 404
    monkeypatch.setattr(article_links, 'settings', SimpleNamespace(environment='local'))
    with TestClient(test_app, base_url='http://127.0.0.1:8090', client=('192.0.2.1', 50000)) as remote:
        assert remote.get(url).status_code == 404


def test_browser_links_reject_changes_to_native_uri():
    target = f'knowledge-inbox://library/{uuid4()}/source/{uuid4()}'
    for value in (target + '?redirect=https://example.test', target + '#fragment', target + '/',
                  target.replace('://library/', '://user@library/'),
                  target.replace('://library/', '://library:123/')):
        assert browser_article_url('http://127.0.0.1:8090', value) is None
