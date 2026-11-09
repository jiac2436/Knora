"""Run using packaged Python with -I -B; isolated database, no worker/model or user files."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
from uuid import uuid4

runtime = Path(sys.argv[1]).resolve()
with TemporaryDirectory(prefix='pka-exact-search-') as directory:
    os.environ['PKA_DATABASE_URL'] = 'sqlite:///' + str(Path(directory) / 'search.sqlite3')
    os.environ['PKA_ENVIRONMENT'] = 'test'
    os.environ['PKA_SEMANTIC_ENABLED'] = 'false'
    sys.path.insert(0, str(runtime / 'backend'))
    from app.database import Base, engine
    from app import models
    from app.main import app
    from app.schemas import SearchRequest
    from app import semantic
    from fastapi.testclient import TestClient
    def forbidden(*args, **kwargs): raise AssertionError('Exact search called a semantic model')
    semantic.embed_query = forbidden
    semantic.semantic_hits = forbidden
    Base.metadata.create_all(engine)
    with TestClient(app) as client:
        session = client.post('/v1/dev/sessions', json={'email': 'synthetic@example.com', 'name': 'Synthetic'}).json()
        headers = {'Authorization': 'Bearer ' + session['access_token']}
        domains = [client.post('/v1/domains', headers=headers, json={'name': 'Synthetic ' + str(i)}).json()['id'] for i in range(2)]
        def capture(title, body, domain):
            response = client.post('/v1/captures', headers={**headers, 'Idempotency-Key': str(uuid4())}, json={
                'capture_type': 'browser_selection', 'title': title, 'content': {'text': body, 'image_urls': []},
                'coverage': 'partial_text', 'context': {'domain_id': domain},
                'client': {'kind': 'desktop', 'version': 'test'}})
            assert response.status_code == 201, response.text
            return response.json()['id']
        wanted = capture('合成原文', '🙂前言。冯玉祥驻守北京，后文。第二处冯玉祥驻守。', domains[0])
        decoy = capture('分散词语', '冯玉祥在其他地方，部队驻守北京。', domains[0])
        capture('另一知识库', '冯玉祥驻守北京', domains[1])
        request = {'query': '冯玉祥驻守', 'mode': 'exact', 'domain_ids': [domains[0]], 'limit': 20}
        result = client.post('/v1/search', headers=headers, json=request)
        assert result.status_code == 200, result.text
        rows = result.json()['items']
        assert [row['source_id'] for row in rows] == [wanted]
        assert rows[0]['locator'] == {'kind': 'text', 'start': 4, 'end': 9}
        assert client.post('/v1/search', headers=headers, json={**request, 'source_id': decoy}).json()['items'] == []
        passages = client.post('/v1/search', headers=headers, json={**request, 'result_unit': 'passage'}).json()['items']
        assert len(passages) == 2 and passages[0]['match_id'] != passages[1]['match_id']
        assert [row['text_locator']['start'] for row in passages] == [4, 18]
        assert all(row['match_text'] == '冯玉祥驻守' for row in passages)
        assert SearchRequest(query='test').result_unit == 'source'
        assert SearchRequest(query='test').mode == 'semantic'
        engine.dispose()
        assert client.post('/v1/search', headers=headers, json=request).json()['items'][0]['source_id'] == wanted
    print('PASS: packaged Python/API, isolated SQLite persistence, literal/scoped/multiple passage search, scalar locator, no model calls')
