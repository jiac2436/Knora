"""Passage-level desktop search keeps source-level Agent clients compatible."""
from dataclasses import replace
import re

import numpy as np
from sqlalchemy import delete, select

from app import semantic, services
from app.database import SessionLocal
from app.models import EvidenceChunk, EvidenceEmbedding, Source, SourceRevision
from app.schemas import SearchRequest
from test_ai_analysis import storage
from test_exact_search import capture, search
from test_summary_retrieval import analyzed


def assert_original_span(row, text):
    locator = row['text_locator']
    assert row['match_text'] == text[locator['start']:locator['end']]
    assert row['match_id'].endswith(f":{locator['start']}:{locator['end']}")


def test_all_literal_occurrences_and_default_source_compatibility(client, account):
    text = '🙂开头。冯玉祥驻守北京。\n\n第二段冯玉祥驻守。\n\n第三段冯玉祥驻守。'
    source_id = capture(client, account, '多个位置', text)
    rows = search(client, account, '冯玉祥驻守', result_unit='passage')
    assert len(rows) == 3 and {r['source_id'] for r in rows} == {source_id}
    assert len({r['match_id'] for r in rows}) == 3
    assert [r['text_locator']['start'] for r in rows] == [m.start() for m in re.finditer('冯玉祥驻守', text)]
    for row in rows:
        assert_original_span(row, text)
    assert len(search(client, account, '冯玉祥驻守')) == 1


def test_article_interleaving_limit_and_scopes(client, account):
    first = capture(client, account, 'A', '目标。目标。目标。目标。')
    second = capture(client, account, 'B', '目标。目标。目标。')
    other = client.post('/v1/domains', headers=account['headers'], json={'name': '另一知识库'}).json()['id']
    capture(client, account, '其他库', '目标。目标。', domain=other)
    options = {'result_unit': 'passage', 'domain_ids': [account['domain']['id']]}
    rows = search(client, account, '目标', limit=3, **options)
    assert len(rows) == 3 and {r['source_id'] for r in rows[:2]} == {first, second}
    assert len(search(client, account, '目标', source_id=first, **options)) == 4
    assert len(search(client, account, '目标', source_id=first, domain_ids=[other], result_unit='passage')) == 0
    stranger = client.post('/v1/dev/sessions', json={'email': 'passage-other@example.test', 'name': 'Other'}).json()
    reply = client.post('/v1/search', headers={'Authorization': 'Bearer ' + stranger['access_token']},
                        json={'query': '目标', 'mode': 'exact', 'source_id': first, 'result_unit': 'passage'})
    assert reply.status_code == 200 and reply.json()['items'] == []
    assert client.delete('/v1/sources/' + first, headers=account['headers']).status_code == 202
    assert {r['source_id'] for r in search(client, account, '目标', **options)} == {second}


def test_pdf_keeps_page_and_independent_text_locator(client, account):
    text = '第一页相关词。\n\n第二页相关词。'
    target = capture(client, account, 'PDF模拟', text)
    with SessionLocal() as db:
        source = db.get(Source, target)
        db.execute(delete(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id))
        for ordinal, piece in enumerate(text.split('\n\n')):
            db.add(EvidenceChunk(revision_id=source.current_revision_id, ordinal=ordinal, text=piece,
                                 locator_kind='pdf_page', locator_start=ordinal + 1, locator_end=ordinal + 1))
        db.commit()
    rows = search(client, account, '相关词', result_unit='passage')
    assert [r['locator']['start'] for r in rows] == [1, 2]
    for row in rows:
        assert row['locator']['kind'] == 'pdf_page'
        assert_original_span(row, text)


def test_semantic_windows_select_original_without_literal_query(storage, monkeypatch):
    db, _, source, chunk, _, _, owner, other = storage
    text = '首段说明城市规划。\n\n末段讨论居住区的建设和布局。'
    revision = db.get(SourceRevision, source.current_revision_id)
    revision.extracted_text = chunk.text = text
    chunk.locator_end = len(text)
    services.reindex_source(db, source)
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True, semantic_min_similarity=0.1))
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    monkeypatch.setattr(semantic, 'embed_query', lambda _: vector)
    for start, end in [(0, 9), (11, len(text))]:
        db.add(EvidenceEmbedding(owner_id=source.owner_id, source_id=source.id, revision_id=revision.id,
            evidence_id=chunk.id, start=start, end=end, model=semantic.MODEL_NAME, vector=vector.tobytes()))
    db.commit()
    request = SearchRequest(query='住宅设计', result_unit='passage')
    rows = services.search_knowledge(db, owner.owner_id, request)
    assert len(rows) == 2
    assert all('住宅设计' not in r['match_text'] for r in rows)
    assert rows[0]['text_locator']['start'] != rows[1]['text_locator']['start']
    for row in rows:
        assert_original_span(row, text)
    assert len(services.search_knowledge(db, owner.owner_id, request.model_copy(update={'result_unit': 'source'}))) == 1
    assert services.search_knowledge(db, other.owner_id, request) == []


def test_summary_citation_selects_original_not_generated_text(storage, monkeypatch):
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))
    analyzed(storage, 'quasar architecture')
    rows = services.search_knowledge(storage[0], storage[6].owner_id, SearchRequest(query='quasar', result_unit='passage'))
    assert len(rows) == 1 and rows[0]['match_sources'] == ['summary']
    assert_original_span(rows[0], storage[3].text)
    assert 'quasar' not in rows[0]['match_text']


def test_semantic_lexical_repetitions_and_title_only(client, account, monkeypatch):
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))
    text = '第一段needle。\n\n第二段needle。\n\n第三段needle。'
    target = capture(client, account, '重复', text)
    rows = search(client, account, 'needle', mode='semantic', result_unit='passage')
    assert len(rows) == 3 and {r['source_id'] for r in rows} == {target}
    for row in rows:
        assert_original_span(row, text)
    capture(client, account, '只在标题中', '完全无关的正文。')
    rows = search(client, account, '只在标题中', result_unit='passage')
    assert len(rows) == 1 and 'match_text' not in rows[0]
