from uuid import uuid4
from sqlalchemy import delete
from conftest import capture_payload
from app.database import SessionLocal
from app.models import EvidenceChunk, Source, SourceRevision


def capture(client, account, title, text, domain=None):
    data = capture_payload(domain or account['domain']['id'], title=title,
                           content={'text': text, 'image_urls': []})
    result = client.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': str(uuid4())}, json=data)
    assert result.status_code == 201, result.text
    return result.json()['id']


def search(client, account, query, **options):
    result = client.post('/v1/search', headers=account['headers'],
                         json={'query': query, 'mode': 'exact', 'limit': 20, **options})
    assert result.status_code == 200, result.text
    return result.json()['items']


def test_literal_not_token_or_summary_and_no_model(client, account, monkeypatch):
    from app import semantic
    def forbidden(*args, **kwargs):
        raise AssertionError('Exact search must never invoke semantic retrieval')
    monkeypatch.setattr(semantic, 'semantic_hits', forbidden)
    monkeypatch.setattr(semantic, 'embed_query', forbidden)
    wanted = capture(client, account, '完整短语', '起始文字。冯玉祥驻守北京。结束文字。')
    capture(client, account, '分散词语', '冯玉祥在其他城市活动，后来部队驻守北京。')
    rows = search(client, account, '冯玉祥驻守')
    assert [r['source_id'] for r in rows] == [wanted]
    assert '冯玉祥驻守' in rows[0]['snippet']
    assert rows[0]['match_sources'] == ['original']
    assert rows[0]['locator']['kind'] == 'text'
    assert not any('summary_snippet' in row for row in rows)


def test_scope_literal_wildcards_and_title(client, account):
    target = capture(client, account, '100%_精确', '本文只有普通内容')
    capture(client, account, '100XX精确', '不应通配匹配')
    other = client.post('/v1/domains', headers=account['headers'], json={'name': '另一个库'}).json()['id']
    other_id = capture(client, account, '100%_精确', '另一知识库', domain=other)
    rows = search(client, account, '100%_', domain_ids=[account['domain']['id']])
    assert [r['source_id'] for r in rows] == [target]
    assert rows[0]['match_sources'] == ['title'] and rows[0]['evidence_id'] is None
    assert search(client, account, '100%_', source_id=target)[0]['source_id'] == target
    assert search(client, account, '100%_', source_id=other_id, domain_ids=[account['domain']['id']]) == []
    assert search(client, account, '100%_', source_id='unknown-source') == []


def test_cross_chunk_locator_current_revision_and_trash(client, account):
    target = capture(client, account, '跨段', '北京历史，保留完整短语')
    with SessionLocal() as db:
        source = db.get(Source, target)
        revision = db.get(SourceRevision, source.current_revision_id)
        db.execute(delete(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id))
        db.add_all([EvidenceChunk(revision_id=revision.id, ordinal=0, text='北京', locator_start=0, locator_end=2),
                    EvidenceChunk(revision_id=revision.id, ordinal=1, text='历史，保留完整短语', locator_start=2, locator_end=12)])
        db.commit()
    row = search(client, account, '北京历史')[0]
    assert row['source_id'] == target and row['locator'] == {'kind': 'text', 'start': 0, 'end': 4}
    with SessionLocal() as db:
        source = db.get(Source, target)
        old = db.get(SourceRevision, source.current_revision_id)
        new = SourceRevision(source_id=target, version=old.version + 1, extracted_text='新的正文')
        db.add(new); db.flush(); source.current_revision_id = new.id; db.commit()
    assert search(client, account, '北京历史') == []
    assert client.delete('/v1/sources/' + target, headers=account['headers']).status_code in (200, 202, 204)
    assert search(client, account, '跨段') == []


def test_matching_precedes_result_limit_and_owner_gate(client, account):
    wanted = capture(client, account, '目标', 'unique phrase ABC')
    for index in range(22):
        capture(client, account, '干扰 ' + str(index), 'unique and phrase are separated')
    assert [r['source_id'] for r in search(client, account, 'unique phrase', limit=1)] == [wanted]
    other = client.post('/v1/dev/sessions', json={'email': 'other@example.com', 'name': 'Other'}).json()
    reply = client.post('/v1/search', headers={'Authorization': 'Bearer ' + other['access_token']},
                        json={'query': 'unique phrase', 'mode': 'exact', 'source_id': wanted})
    assert reply.status_code == 200 and reply.json()['items'] == []
    assert client.post('/v1/search', headers=account['headers'],
                       json={'query': 'x', 'mode': 'unsupported'}).status_code == 422


def test_title_remains_searchable_before_evidence_is_parsed(client, account):
    target = capture(client, account, '待解析附件标题', '临时内容')
    with SessionLocal() as db:
        source = db.get(Source, target)
        revision = db.get(SourceRevision, source.current_revision_id)
        revision.extracted_text = ''
        db.execute(delete(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id))
        db.commit()
    rows = search(client, account, '待解析附件')
    assert [row['source_id'] for row in rows] == [target]
    assert rows[0]['locator']['kind'] == 'title' and rows[0]['evidence_id'] is None
