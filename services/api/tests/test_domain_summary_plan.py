"""Adaptive summary-first planning; no real model or personal data."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from app import domain_builds as builds
from app import domain_overview_plan as adaptive
from app.domain_contract import DomainSummaryRequest, summary_provider_input, SUMMARY_INPUT_BUDGET
from app.domain_summary_jobs import current_node_result
from app.domain_retrieval import node_hits
from app.domain_topics import source_scope
from app.errors import ApiError
from app.models import Source, DomainSummaryArtifact
from test_ai_analysis import storage
from test_domain_summary_jobs import prepared
from test_domain_builds import add_documents, enqueue, finish, gateway
from test_summary_retrieval import analyzed
from test_domain_summary_contract import request_data, result_data, client, provider, AUTH


@pytest.fixture
def corpus(prepared, monkeypatch):
    monkeypatch.setattr(builds, 'settings', replace(builds.settings,
        ai_gateway_url='http://127.0.0.1:8765', ai_gateway_token='synthetic-secret'))
    return prepared


def test_27_documents_use_one_overview_call_and_reuse_root(corpus):
    add_documents(corpus, 26)
    build, plan = enqueue(corpus)
    assert len(plan['steps']) == builds.plan_budget(plan)['calls'] == 1
    request = DomainSummaryRequest.model_validate(plan['steps'][0]['frozen']['request'])
    assert request.contract_version == 2 and request.purpose == 'overview'
    assert len(request.summaries) == 27
    assert len({s.source_ids[0] for s in request.summaries}) == 27
    calls = []; finish(corpus, calls)
    assert build.status == 'completed' and len(calls) == 1
    db, _, source = corpus[:3]
    assert current_node_result(db, source.owner_id, source.domain_id, 'app')['available']
    next_build, next_plan = enqueue(corpus)
    assert builds.plan_budget(next_plan)['calls'] == 0
    repeated = []; finish(corpus, repeated)
    assert next_build.status == 'completed' and repeated == []
    # The new node remains a retrieval hint resolving actual originals.
    hits = node_hits(db, source.owner_id, '知识加工', ['知识加工'],
                     source_scope(source.owner_id, source.domain_id, 'app'))
    assert hits and all(row[1].text for row in hits.values())


def test_large_input_is_batched_once_per_document_and_uses_broad_merges(corpus, monkeypatch):
    add_documents(corpus, 99)
    original = adaptive.summary_document
    def long_summary(db, dependency):
        context, evidence, reason = original(db, dependency)
        return context.model_copy(update={'text': '不同知识方向及其条件。' * 150}), evidence, reason
    monkeypatch.setattr(adaptive, 'summary_document', long_summary)
    build, plan = enqueue(corpus)
    leaves = [s for s in plan['steps'] if not s['children']]
    assert len(leaves) > 1
    ids = [i for step in leaves for i in step['source_ids']]
    assert len(ids) == len(set(ids)) == 100
    assert plan['coverage']['all_valid_summaries_processed']
    assert any(len(s['children']) > 2 for s in plan['steps'])
    for step in leaves:
        req = DomainSummaryRequest.model_validate(step['frozen']['request'])
        assert req.purpose == 'batch'
        assert step['input_tokens'] <= SUMMARY_INPUT_BUDGET
    calls = []; finish(corpus, calls)
    assert build.status == 'completed'
    # A document edit changes only its leaf, plus ancestors; stable peers reuse.
    source = corpus[2]
    source.title = '变更后的资料'; corpus[0].commit(); analyzed(corpus)
    next_plan = builds.prepare_plan(corpus[0], source.owner_id, source.domain_id, 'app')
    reused = [s for s in next_plan['steps'] if s['reuse_id']]
    assert reused and len(reused) >= len(leaves) - 1


def test_summary_gateway_does_not_send_original_text_and_caches(corpus):
    data = request_data(); data['contract_version'] = 2
    data['evidence'][0]['text'] = '不应发送到模型的原文秘密'
    data['evidence'][0]['text_end'] = len(data['evidence'][0]['text'])
    upstream_calls = []
    def upstream(request):
        payload = json.loads(request.content); upstream_calls.append(payload)
        assert '不应发送到模型的原文秘密' not in request.content.decode()
        body = json.loads(payload['messages'][1]['content'])
        assert 'evidence' not in body and len(body['summaries']) == 2
        assert payload['thinking'] == {'type': 'disabled'}
        return provider(result_data(data))
    with client(upstream) as http:
        first = http.post('/v1/summarize-node', json=data, headers=AUTH)
        assert first.status_code == 200, first.text
        assert http.post('/v1/summarize-node', json=data, headers=AUTH).json() == first.json()
    assert len(upstream_calls) == 1


def test_changed_document_invalidates_published_navigation(corpus):
    build, _ = enqueue(corpus); calls = []; finish(corpus, calls)
    db, _, source = corpus[:3]
    source.title = '已修改'; db.commit()
    assert not current_node_result(db, source.owner_id, source.domain_id, 'app')['available']


def test_progress_tracks_actual_nodes_and_failure_stops(corpus):
    import httpx
    build, _ = enqueue(corpus)
    assert builds.build_view(corpus[0], build)['completed_steps'] == 0
    calls = []; finish(corpus, calls, httpx.MockTransport(lambda _: httpx.Response(504)))
    assert build.status == 'unknown' and builds.build_view(corpus[0], build)['completed_steps'] == 0
    assert calls == []
    from services.worker.domain_build import run_build_once
    assert not run_build_once(corpus[0])


def test_worst_case_unicode_parent_fits_input_and_local_envelope():
    from uuid import uuid4
    from app.domain_contract import DomainContextSummary, DomainEvidence, summary_input_reservation, SUMMARY_WIRE_BUDGET
    from app.domain_overview_plan import FAN_IN
    contexts, originals = [], []
    for _ in range(FAN_IN):
        entries = [DomainEvidence(id=str(uuid4()), source_id=str(uuid4()), revision_id=str(uuid4()),
            text='😀' * 1000, text_start=0, text_end=1000, locator_kind='text', locator_start=0, locator_end=1000)
            for _ in range(70)]  # Seven output statements, ten anchors each.
        originals.extend(entries)
        contexts.append(DomainContextSummary(node_id=str(uuid4()), kind='topic', text='😀' * 2200 + '\n' * 6,
            citations=[e.id for e in entries], source_ids=[e.source_id for e in entries]))
    request = DomainSummaryRequest(contract_version=2, request_id=str(uuid4()), node_id=str(uuid4()),
        kind='domain', purpose='batch', title='😀' * 120, summaries=contexts, evidence=originals)
    assert summary_input_reservation(request) <= SUMMARY_INPUT_BUDGET
    assert len(request.model_dump_json().encode()) <= SUMMARY_WIRE_BUDGET


def test_oversized_provider_output_is_not_published_or_replayed():
    data = request_data(); data['contract_version'] = 2; data['kind'] = 'domain'
    calls = []
    def upstream(request):
        calls.append(request)
        result = result_data(data); result['overview']['text'] = '字' * 501
        return provider(result)
    with client(upstream) as http:
        first = http.post('/v1/summarize-node', json=data, headers=AUTH)
        second = http.post('/v1/summarize-node', json=data, headers=AUTH)
        assert first.status_code == second.status_code == 502
        assert first.json()['detail']['reason'] == 'node_reading_budget'
    assert len(calls) == 1


def navigation_data(count):
    """Synthetic original bindings; no knowledge-library contents or credentials."""
    from uuid import uuid4
    data = request_data()
    data['contract_version'] = 2
    data['evidence'] = [dict(data['evidence'][0], id=str(uuid4()), source_id=str(uuid4()),
                             revision_id=str(uuid4())) for _ in range(count)]
    data['summaries'] = [dict(data['summaries'][0], kind='topic',
        citations=[e['id'] for e in data['evidence']],
        source_ids=[e['source_id'] for e in data['evidence']])]
    return data


@pytest.mark.parametrize('citations', [4, 10])
def test_navigation_accepts_valid_citations_up_to_contract_limit(citations):
    data = navigation_data(citations)
    calls = []
    with client(lambda request: calls.append(request) or provider(result_data(data))) as http:
        first = http.post('/v1/summarize-node', json=data, headers=AUTH)
        repeated = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert first.status_code == 200, first.text
    assert first.json() == repeated.json() and len(calls) == 1
    assert len(first.json()['result']['overview']['citations']) == citations
    prompt = json.loads(calls[0].content)['messages'][0]['content']
    assert '硬上限10个' in prompt and '输出contract_version固定为1' in prompt


@pytest.mark.parametrize('case', ['too_many', 'unknown', 'duplicate'])
def test_navigation_keeps_citation_integrity_checks(case):
    data = navigation_data(11 if case == 'too_many' else 4)
    result = result_data(data)
    if case == 'unknown':
        result['overview']['citations'][0] = 'invented'
    elif case == 'duplicate':
        result['overview']['citations'][0] = result['overview']['citations'][1]
    calls = []
    with client(lambda request: calls.append(request) or provider(result)) as http:
        first = http.post('/v1/summarize-node', json=data, headers=AUTH)
        repeated = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert first.status_code == repeated.status_code == 502
    assert len(calls) == 1
    if case != 'too_many':
        assert first.json()['detail']['reason'] == 'node_evidence_reference'


def test_large_navigation_snapshot_stays_local_and_fits_wire_budget():
    from app.domain_contract import SUMMARY_WIRE_BUDGET
    from services.ai_gateway.main import MAX_REQUEST_BYTES
    data = navigation_data(180)
    for item in data['evidence']:
        item.update(text='😀' * 1000, text_end=1000)
    body = json.dumps(data, ensure_ascii=False).encode()
    assert MAX_REQUEST_BYTES < len(body) < SUMMARY_WIRE_BUDGET
    calls = []
    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        content = json.loads(payload['messages'][1]['content'])
        assert set(content['summaries'][0]) == {'text', 'citations'}
        assert 'references' not in content and 'evidence' not in content
        assert data['evidence'][0]['source_id'] not in request.content.decode()
        assert '😀' not in payload['messages'][1]['content']
        result = result_data(data)
        result['overview']['citations'] = result['overview']['citations'][:10]
        return provider(result)
    with client(upstream) as http:
        reply = http.post('/v1/summarize-node', content=body,
                          headers=dict(AUTH, **{'Content-Type': 'application/json'}))
    assert reply.status_code == 200, reply.text
    assert len(calls) == 1


@pytest.mark.parametrize('route', ['/v1/analyze', '/v1/answer', '/v1/summarize-node'])
def test_other_requests_keep_existing_wire_limit(route):
    from services.ai_gateway.main import MAX_REQUEST_BYTES
    data = request_data()
    body = b' ' * MAX_REQUEST_BYTES + json.dumps(data).encode()
    calls = []
    with client(lambda request: calls.append(request) or provider({})) as http:
        reply = http.post(route, content=body, headers=AUTH)
    assert reply.status_code == 413 and calls == []


def test_navigation_wire_and_original_window_limits_reject_before_provider():
    from app.domain_contract import SUMMARY_WIRE_BUDGET
    data = navigation_data(2)
    calls = []
    with client(lambda request: calls.append(request) or provider({})) as http:
        reply = http.post('/v1/summarize-node', content=b' ' * (SUMMARY_WIRE_BUDGET + 1), headers=AUTH)
        assert reply.status_code == 413
        data['evidence'][0].update(text='字' * 1001, text_end=1001)
        assert http.post('/v1/summarize-node', json=data, headers=AUTH).status_code == 422
    assert calls == []


def test_health_reports_only_readonly_contract_metadata():
    from services.ai_gateway.main import SUMMARY_PROMPT_VERSION
    calls = []
    with client(lambda request: calls.append(request) or provider({})) as http:
        for _ in range(3):
            reply = http.get('/health')
            assert reply.status_code == 200
            assert reply.json() == dict(ready=True, node_contract_versions=[1, 2],
                                        summary_prompt_version=SUMMARY_PROMPT_VERSION,
                                        analysis_image_support=False)
    assert calls == []


def test_final_output_budget_allows_full_legal_citation_envelope():
    data = navigation_data(70)
    data['kind'] = 'domain'
    references = [e['id'] for e in data['evidence']]
    points = [dict(text='😀' * 69 + str(i), citations=references[i * 10:(i + 1) * 10])
              for i in range(7)]
    result = dict(contract_version=1, overview=points[0], insights=points[1:4], limitations=points[4:])
    calls = []
    with client(lambda request: calls.append(json.loads(request.content)) or provider(result)) as http:
        reply = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert reply.status_code == 200, reply.text
    # The old 2,048 ceiling could truncate legal UUID lists. This is a
    # conservative byte bound, not an estimate of actual provider billing.
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= calls[0]['max_tokens'] == 8192


@pytest.mark.parametrize('case', ['schema', 'citation', 'budget'])
def test_node_failure_log_contains_safe_diagnostics_only(caplog, case):
    data = navigation_data(2)
    secret = 'synthetic-private-note-or-provider-output'
    result = result_data(data)
    if case == 'schema':
        result[secret] = secret
    elif case == 'citation':
        result['overview']['citations'] = [secret]
    else:
        data['kind'] = 'domain'
        result['overview']['text'] = secret * 30
    with client(lambda _: provider(result)) as http:
        reply = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert reply.status_code == 502
    assert 'node_result_rejected' in caplog.text
    assert data['request_id'] in caplog.text
    assert 'stage=' in caplog.text and 'reason=' in caplog.text
    assert secret not in caplog.text + reply.text
    assert 'synthetic-gateway' not in caplog.text
    assert 'synthetic-provider' not in caplog.text
