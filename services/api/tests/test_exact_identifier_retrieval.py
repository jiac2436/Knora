"""Exact identifiers in current originals outrank ambiguous semantic neighbors."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import semantic, services
from app.models import EvidenceChunk, PersonalContext, Source, SourceRevision, utcnow
from app.schemas import SearchRequest
from test_ai_analysis import storage
from test_domain_builds import corpus
from test_domain_summary_jobs import prepared
from test_domain_retrieval import build as build_node
from test_summary_retrieval import analyzed


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))


def document(data, title, text, *, identifier=None, domain=None, visibility='app_and_agents'):
    db, _, original = data[:3]
    source = Source(owner_id=original.owner_id, domain_id=domain or original.domain_id,
        title=title, kind='text', coverage='full_text', visibility=visibility)
    if identifier:
        source.id = identifier
    db.add(source); db.flush()
    revision = SourceRevision(source_id=source.id, version=1, extracted_text=text)
    db.add(revision); db.flush(); source.current_revision_id = revision.id
    evidence = EvidenceChunk(revision_id=revision.id, ordinal=0, text=text, locator_end=len(text))
    db.add(evidence); db.flush(); services.reindex_source(db, source); db.commit(); db.expire_all()
    return source, evidence, revision


def search(data, query, **options):
    return services.search_knowledge(data[0], data[2].owner_id, SearchRequest(query=query, **options))


def semantic_neighbors(monkeypatch, rows):
    monkeypatch.setattr(semantic, 'semantic_hits', lambda *args, **kwargs: [
        (0.99, source, evidence, revision, SimpleNamespace(start=0))
        for source, evidence, revision in rows])


@pytest.mark.parametrize('location', ['title', 'body'])
def test_identifier_survives_keyword_cap_and_semantic_rrf(storage, monkeypatch, location):
    # A shared prefix makes dozens of distractors both lexical and highly semantic.
    neighbors = [document(storage, f'邻居 {i}', '性能案例编号 Q00001。', identifier=f'neighbor-{i:03}')
                 for i in range(70)]
    target = document(storage, '性能案例 Q0000' if location == 'title' else '性能案例',
        '性能案例编号 Q0000。' if location == 'body' else '性能案例具体原文。', identifier='zz-exact')
    semantic_neighbors(monkeypatch, neighbors[:60])
    rows = search(storage, 'Q0000 性能案例', limit=3)
    assert rows[0]['source_id'] == target[0].id
    assert rows[0]['evidence_id'] == target[1].id
    assert 'original' in rows[0]['match_sources']


@pytest.mark.parametrize('identifier', ['RFC9110', 'ERR-402', 'SKU-A12', 'Q0000', 'v1.2.3', '1.2.3'])
def test_casefold_and_complete_identifier_boundaries_next_to_chinese(storage, identifier):
    target = document(storage, '目标', f'采用{identifier.lower()}处理。', identifier='zz-exact')
    document(storage, '前缀相同', f'采用{identifier}1处理。', identifier='aa-prefix')
    rows = search(storage, identifier)
    assert [row['source_id'] for row in rows] == [target[0].id]
    assert identifier.lower() in rows[0]['snippet'].lower()


def test_exact_identifier_chooses_matching_chunk_not_shared_prose(storage, monkeypatch):
    source, first, revision = document(storage, '性能案例', '性能案例的常见介绍。')
    evidence = EvidenceChunk(revision_id=revision.id, ordinal=1, text='末尾记录 ERR-402。', locator_start=100, locator_end=115)
    storage[0].add(evidence)
    revision.extracted_text += '\n' + evidence.text
    services.reindex_source(storage[0], source); storage[0].commit()
    rows = search(storage, 'ERR-402 性能案例')
    assert rows[0]['evidence_id'] == evidence.id
    assert 'ERR-402' in rows[0]['snippet']
    assert rows[0]['locator']['start'] >= 100


@pytest.mark.parametrize('restriction', ['private', 'deleted', 'deleting', 'cross_domain', 'other_owner', 'project', 'tag'])
def test_exact_priority_stays_inside_existing_scope(storage, restriction):
    source, _, _ = document(storage, 'ERR-402', 'ERR-402 的原文')
    db, _, original, _, other_domain, project = storage[:6]
    options = {}
    if restriction == 'private': source.visibility = 'app_only'
    elif restriction == 'deleted': source.deleted_at = utcnow()
    elif restriction == 'deleting': source.processing_status = 'deleting'
    elif restriction == 'cross_domain': source.domain_id = other_domain.id
    elif restriction == 'other_owner': source.owner_id = storage[7].owner_id
    elif restriction == 'project': options['project_id'] = project.id
    elif restriction == 'tag': options['tag_query'] = '不存在的标签'
    db.commit()
    rows = services.search_knowledge(db, original.owner_id, SearchRequest(query='ERR-402', **options),
        allowed_domains={original.domain_id}, for_agent=True)
    assert rows == []


def test_summary_identifier_is_retrieval_hint_not_exact_original_priority(storage, monkeypatch):
    artifact = analyzed(storage, summary='Q0000 性能案例')
    target = document(storage, '目标', 'Q0000 原文', identifier='zz-exact')
    original = storage[2]
    revision = storage[0].get(SourceRevision, original.current_revision_id)
    semantic_neighbors(monkeypatch, [(original, storage[3], revision)])
    rows = search(storage, 'Q0000', limit=2)
    assert rows[0]['source_id'] == target[0].id
    generated = next(row for row in rows if row['source_id'] == original.id)
    assert generated['summary_artifact_id'] == artifact.id
    assert 'Q0000' not in generated['snippet']


def test_node_identifier_never_claims_original_match(corpus):
    build_node(corpus, text='编号 Q0000 的概览。')
    rows = search(corpus, 'Q0000')
    assert rows and rows[0]['match_sources'] == ['knowledge_node']
    assert 'Q0000' not in rows[0]['snippet']
    assert rows[0]['requires_evidence_confirmation']


def test_context_identifier_does_not_outrank_original_identifier(storage, monkeypatch):
    source, evidence, revision = document(storage, '性能案例', '普通原文')
    storage[0].add(PersonalContext(source_id=source.id, notes='ERR-402'))
    services.reindex_source(storage[0], source); storage[0].commit()
    target = document(storage, '目标', 'ERR-402 原文', identifier='zz-exact')
    semantic_neighbors(monkeypatch, [(source, evidence, revision)])
    rows = search(storage, 'ERR-402', limit=2)
    assert rows[0]['source_id'] == target[0].id


def test_natural_language_still_fuses_semantic_channels(storage, monkeypatch):
    lexical = document(storage, '性能', '性能的一般描述', identifier='aa-lexical')
    hybrid = document(storage, '性能', '性能的实践经验', identifier='zz-hybrid')
    semantic_neighbors(monkeypatch, [hybrid])
    rows = search(storage, '性能')
    assert rows[0]['source_id'] == hybrid[0].id and rows[0]['match_type'] == 'hybrid'


def test_snippet_locator_skip_earlier_longer_identifier(storage):
    text = 'Q00001 是另一编号。' + '普通背景材料。' * 80 + '真正的问题是 Q0000。'
    target = document(storage, '目标', text)
    rows = search(storage, 'Q0000')
    assert rows[0]['evidence_id'] == target[1].id
    assert rows[0]['locator']['start'] == text.rindex('Q0000')
    assert 'Q0000' in rows[0]['snippet'] and 'Q00001' not in rows[0]['snippet']


@pytest.mark.parametrize('query', ['ERR-402.', '(err-402)', '请解释ERR-402'])
def test_punctuation_and_chinese_wrapped_identifier_queries(storage, query):
    target = document(storage, '目标', '问题编号 ERR-402。')
    document(storage, '无关编号', 'ERR-4021', identifier='aa-prefix')
    rows = search(storage, query)
    assert rows[0]['source_id'] == target[0].id


def test_title_identifier_still_prefers_original_code_bearing_chunk(storage):
    source, _, revision = document(storage, '性能案例 ERR-402', '性能案例的常见介绍。')
    evidence = EvidenceChunk(revision_id=revision.id, ordinal=1, text='日志说明 ERR-402。', locator_start=100, locator_end=115)
    storage[0].add(evidence); revision.extracted_text += '\n' + evidence.text
    services.reindex_source(storage[0], source); storage[0].commit()
    rows = search(storage, 'ERR-402 性能案例')
    assert rows[0]['evidence_id'] == evidence.id and 'ERR-402' in rows[0]['snippet']


@pytest.mark.parametrize('location', ['title', 'body'])
@pytest.mark.parametrize('identifier', ['ERR-402', 'PROTOCOL12345678901234567890123456789012345'])
def test_original_sentence_period_uses_existing_index_without_rebuild(storage, location, identifier):
    title = f'Failure {identifier}.' if location == 'title' else '普通标题'
    text = f'Failure {identifier}.' if location == 'body' else '这是原文。'
    target = document(storage, title, text)
    # reindex_source uses the unchanged historical token/prefix representation.
    rows = search(storage, identifier)
    assert rows and rows[0]['source_id'] == target[0].id
    assert rows[0]['evidence_id'] == target[1].id


def test_same_block_snippet_prioritizes_identifier_over_dense_common_terms(storage):
    text = '性能案例性能案例的介绍。' + '一般背景。' * 100 + '日志记录 ERR-402。'
    target = document(storage, '性能案例', text)
    rows = search(storage, 'ERR-402 性能案例')
    assert rows[0]['evidence_id'] == target[1].id
    assert rows[0]['locator']['start'] == text.index('ERR-402')
    assert 'ERR-402' in rows[0]['snippet']
