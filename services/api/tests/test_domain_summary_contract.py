"""Multi-source contract and gateway tests; synthetic transport only."""
import copy
from dataclasses import replace
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.domain_contract import DomainSummaryRequest, DomainSummaryResult, DomainSummaryResponse, validate_domain_summary
from app.domain_summary_inputs import build_node_request, validate_node_publication
from app.domain_inputs import freeze_domain_inputs, manifest_fingerprint
from app.errors import ApiError
from app.models import Source, utcnow
from services.ai_gateway import main as gateway
from services.worker import domain_summary as worker
from test_ai_analysis import storage
from test_summary_retrieval import analyzed


AUTH = {'Authorization': 'Bearer synthetic-gateway'}


def request_data():
    first, second = str(uuid4()), str(uuid4())
    evidence = [dict(id=str(uuid4()), source_id=s, revision_id=str(uuid4()),
        text='合成原文论据。', text_start=0, text_end=7, locator_kind='paragraph', locator_start=0, locator_end=1)
        for s in (first, second)]
    return dict(request_id=str(uuid4()), node_id=str(uuid4()), kind='topic', title='知识检索',
        summaries=[dict(node_id=str(uuid4()), kind='document', text='合成摘要。',
            source_ids=[e['source_id']], citations=[e['id']]) for e in evidence], evidence=evidence)


def result_data(data):
    return dict(contract_version=1, overview=dict(text='资料讨论知识检索。', citations=[e['id'] for e in data['evidence']]),
                insights=[], limitations=[])


def provider(result):
    return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(result)))],
                                        usage=dict(prompt_tokens=10, completion_tokens=5)))


def client(handler):
    return TestClient(gateway.create_app(gateway.GatewaySettings('synthetic-provider', 'test-model', 'synthetic-gateway'),
                                         transport=httpx.MockTransport(handler)))


def test_authenticated_node_request_is_cached_and_uses_bounded_prompt():
    data, calls = request_data(), []
    def upstream(request):
        calls.append(json.loads(request.content))
        return provider(result_data(data))
    with client(upstream) as http:
        assert http.post('/v1/summarize-node', json=data).status_code == 401
        first = http.post('/v1/summarize-node', json=data, headers=AUTH)
        assert first.status_code == 200, first.text
        assert first.json()['request_id'] == data['request_id']
        assert first.json()['prompt_version'] == gateway.DOMAIN_PROMPT_VERSION
        assert http.post('/v1/summarize-node', json=data, headers=AUTH).json() == first.json()
        changed = dict(data, title='Changed')
        assert http.post('/v1/summarize-node', json=changed, headers=AUTH).status_code == 409
    assert len(calls) == 1
    assert calls[0]['max_tokens'] == 16384 and calls[0]['thinking'] == {'type': 'enabled'}
    assert calls[0]['reasoning_effort'] == 'high'
    assert '不可信数据' in calls[0]['messages'][0]['content']
    assert 'summaries仅辅助组织' in calls[0]['messages'][0]['content']
    assert '不声称读完整个知识域' in calls[0]['messages'][0]['content']


def test_reasoning_is_not_exposed_or_persisted_in_cached_node_response():
    data = request_data()
    def upstream(_):
        body = json.loads(provider(result_data(data)).content)
        body['choices'][0]['message']['reasoning_content'] = 'synthetic-private-reasoning'
        return httpx.Response(200, json=body)
    with client(upstream) as http:
        first = http.post('/v1/summarize-node', json=data, headers=AUTH)
        second = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert 'synthetic-private-reasoning' not in first.text + second.text


@pytest.mark.parametrize('case', ['reference', 'repeated', 'budget', 'blank', 'missing_citation', 'extra', 'version'])
def test_invalid_node_output_is_rejected_without_exposing_model_content(case):
    data = request_data()
    result = result_data(data)
    if case == 'reference': result['overview']['citations'] = ['invented']
    elif case == 'repeated': result['insights'] = [copy.deepcopy(result['overview'])]
    elif case == 'budget': result['overview']['text'] = '字' * 800; result['insights'] = [dict(text='超额', citations=[data['evidence'][0]['id']])]
    elif case == 'blank': result['overview']['text'] = ' '
    elif case == 'missing_citation': result['overview']['citations'] = []
    elif case == 'version': result['contract_version'] = 2
    else: result['secret'] = 'synthetic-private-output'
    with client(lambda _: provider(result)) as http:
        reply = http.post('/v1/summarize-node', json=data, headers=AUTH)
        assert reply.status_code == 502
        assert 'synthetic-private-output' not in reply.text


@pytest.mark.parametrize('case', ['source', 'revision', 'duplicate', 'orphan', 'oversize', 'loop'])
def test_invalid_context_is_rejected_before_upstream(case):
    data, calls = request_data(), []
    if case == 'source': data['summaries'][0]['source_ids'] = [str(uuid4())]
    elif case == 'revision': data['evidence'][1]['source_id'] = data['evidence'][0]['source_id']; data['summaries'][1]['source_ids'] = [data['evidence'][0]['source_id']]
    elif case == 'duplicate': data['summaries'][1]['node_id'] = data['summaries'][0]['node_id']
    elif case == 'orphan': data['summaries'] = data['summaries'][:1]
    elif case == 'oversize': data['evidence'][0].update(text='字' * 3001, text_end=3001)
    else: data['summaries'][0]['kind'] = 'topic'
    with client(lambda req: calls.append(req) or provider({})) as http:
        assert http.post('/v1/summarize-node', json=data, headers=AUTH).status_code == 422
    assert calls == []


def test_domain_has_a_smaller_reading_budget_and_can_use_original_bound_topic_context():
    data = request_data()
    data['kind'] = 'domain'; data['summaries'][0]['kind'] = 'topic'
    request = DomainSummaryRequest.model_validate(data)
    output = result_data(data)
    output['overview']['text'] = '字' * 501
    with pytest.raises(ValueError, match='reading budget'):
        validate_domain_summary(request, DomainSummaryResult.model_validate(output))


def test_unknown_provider_call_is_cached_and_never_automatically_replayed():
    data, calls = request_data(), []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('synthetic-private-error')
    with client(timeout) as http:
        for _ in range(2):
            response = http.post('/v1/summarize-node', json=data, headers=AUTH)
            assert response.status_code == 504
            assert 'synthetic-private-error' not in response.text
    assert len(calls) == 1


def test_node_builder_uses_valid_summary_and_exact_original_text(storage):
    analyzed(storage, 'Synthetic summary')
    db, _, source, chunk, *_ = storage
    before = manifest_fingerprint(freeze_domain_inputs(db, source.owner_id, source.domain_id))
    frozen = build_node_request(db, source.owner_id, source.domain_id, 'app', uuid4())
    wire = DomainSummaryRequest.model_validate(frozen['request'])
    assert wire.evidence[0].text == chunk.text
    assert wire.evidence[0].original_evidence_id == chunk.id
    assert frozen['coverage']['included_source_count'] == 1
    assert frozen['coverage']['original_text_complete'] is False
    assert manifest_fingerprint(freeze_domain_inputs(db, source.owner_id, source.domain_id)) == before
    source.visibility = 'app_only'; db.commit()
    with pytest.raises(ApiError):
        build_node_request(db, source.owner_id, source.domain_id, 'shareable', uuid4())


def test_node_builder_does_not_trigger_analysis_for_unanalyzed_or_stale_sources(storage):
    db, _, source, *_ = storage
    with pytest.raises(ApiError) as error:
        build_node_request(db, source.owner_id, source.domain_id, 'app', uuid4())
    assert error.value.code == 'no_eligible_summary'
    analyzed(storage, 'Synthetic summary')
    source.title = 'Changed after analysis'; db.commit()
    with pytest.raises(ApiError): build_node_request(db, source.owner_id, source.domain_id, 'app', uuid4())


def test_partial_node_coverage_includes_unanalyzed_sources_in_denominator(storage):
    analyzed(storage, 'Synthetic summary')
    db, _, source, *_ = storage
    unprocessed = Source(owner_id=source.owner_id, domain_id=source.domain_id,
                         kind='text', title='Not analyzed', coverage='full_text')
    db.add(unprocessed); db.commit()
    frozen = build_node_request(db, source.owner_id, source.domain_id, 'app', uuid4())
    assert frozen['coverage']['source_count'] == 2
    assert frozen['coverage']['included_source_count'] == 1
    assert frozen['coverage']['excluded'][unprocessed.id] == 'no_valid_summary'
    assert frozen['coverage']['all_sources_selected'] is False


@pytest.mark.parametrize('change', ['none', 'title', 'delete', 'visibility', 'move', 'request_id'])
def test_publication_guard_requires_current_inputs_and_request_binding(storage, change):
    analyzed(storage, 'Synthetic summary')
    db, _, source, _, target, *_ = storage
    owner, domain = source.owner_id, source.domain_id
    frozen = build_node_request(db, owner, domain, 'shareable', uuid4())
    response = DomainSummaryResponse(request_id=frozen['request']['request_id'],
        result=DomainSummaryResult.model_validate(result_data(frozen['request'])), provider='synthetic',
        model='model', prompt_version='test-v1', usage={'prompt_tokens': 10, 'completion_tokens': 5})
    if change == 'title': source.title = 'Changed'
    elif change == 'delete': source.deleted_at = utcnow()
    elif change == 'visibility': source.visibility = 'app_only'
    elif change == 'move': source.domain_id = target.id
    elif change == 'request_id': response.request_id = str(uuid4())
    db.commit()
    if change == 'none':
        validate_node_publication(db, owner, domain, 'shareable', frozen, response)
    else:
        with pytest.raises(ApiError):
            validate_node_publication(db, owner, domain, 'shareable', frozen, response)
    db.rollback()


@pytest.mark.parametrize('status', [401, 502, 504])
def test_worker_transport_calls_once_and_classifies_failures(monkeypatch, status):
    monkeypatch.setattr(worker, 'settings', replace(worker.settings,
        ai_gateway_url='http://127.0.0.1:8091', ai_gateway_token='synthetic'))
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={'detail': {'code': 'provider_result_unknown'}})
    result, _, response = worker.call_node_gateway(DomainSummaryRequest.model_validate(request_data()), httpx.MockTransport(handle))
    assert result == ('failed' if status == 401 else 'unknown')
    assert response is None and len(calls) == 1


def test_worker_accepts_valid_bound_response_and_rejects_wrong_request_id(monkeypatch):
    monkeypatch.setattr(worker, 'settings', replace(worker.settings,
        ai_gateway_url='http://127.0.0.1:8091', ai_gateway_token='synthetic'))
    data = request_data()
    response = dict(request_id=data['request_id'], result=result_data(data), provider='synthetic',
                    model='model', prompt_version='test-v1', usage={'prompt_tokens': 10, 'completion_tokens': 5})
    request = DomainSummaryRequest.model_validate(data)
    assert worker.call_node_gateway(request, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))[0] == 'completed'
    response['request_id'] = str(uuid4())
    assert worker.call_node_gateway(request, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))[0] == 'failed'


def test_short_originals_have_adaptive_compression_guidance_without_changing_wire_contract():
    data = request_data()
    calls = []
    with client(lambda req: calls.append(json.loads(req.content)) or provider(result_data(data))) as http:
        reply = http.post('/v1/summarize-node', json=data, headers=AUTH)
        assert reply.status_code == 200
    original_size = sum(len(e['text']) for e in data['evidence'])
    prompt = calls[0]['messages'][0]['content']
    assert f'目标不超过{original_size}字符' in prompt
    assert '压缩不允许改变事实' in prompt
    assert '不要换句话重复概览' in prompt
    assert reply.json()['prompt_version'] == 'domain-node-evidence-v2.6'
    wire = json.loads(calls[0]['messages'][1]['content'])
    assert DomainSummaryRequest.model_validate(wire).contract_version == 1


def test_long_originals_keep_bounded_topic_and_domain_targets():
    data = request_data()
    for e in data['evidence']:
        e['text'] = '合成原文。' * 400
        e['text_end'] = len(e['text'])
    request = DomainSummaryRequest.model_validate(data)
    assert '目标不超过480字符' in gateway.domain_prompt_for(request)
    request.kind = 'domain'
    assert '目标不超过300字符' in gateway.domain_prompt_for(request)


def test_attribution_prompt_requires_original_context_even_when_summaries_agree():
    data = request_data()
    original = '词甲的搜索量为100；词乙的难度为3。这个词适合练习。'
    data['evidence'][0].update(text=original, text_end=len(original))
    for summary in data['summaries']:
        summary['text'] = '词甲的难度为3。'
    calls = []
    with client(lambda req: calls.append(json.loads(req.content)) or provider(result_data(data))) as http:
        response = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert response.status_code == 200
    prompt = calls[0]['messages'][0]['content']
    assert '不能把重复当作验证' in prompt
    assert '不把案例A的数值或原因移给案例B' in prompt
    assert 'text_start顺序' in prompt and 'OCR表格的行列可能错乱' in prompt
    wire = json.loads(calls[0]['messages'][1]['content'])
    assert next(e for e in wire['evidence'] if e['id'] == data['evidence'][0]['id'])['text'] == original
    assert wire['summaries'][0]['text'] == data['summaries'][0]['text']


def test_provider_reads_contiguous_original_windows_without_changing_wire_contract():
    data = request_data()
    following = []
    for context, entry in zip(data['summaries'], data['evidence']):
        entry['original_evidence_id'] = entry['source_id']
        next_entry = dict(entry, id=str(uuid4()), text_start=len(entry['text']), text_end=2 * len(entry['text']))
        context['citations'].append(next_entry['id']); following.append(next_entry)
    data['evidence'].extend(following)
    calls = []
    with client(lambda req: calls.append(json.loads(req.content)) or provider(result_data(data))) as http:
        reply = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert reply.status_code == 200
    original = DomainSummaryRequest.model_validate(data)
    wire = DomainSummaryRequest.model_validate_json(calls[0]['messages'][1]['content'])
    assert {e.id: e for e in wire.evidence} == {e.id: e for e in original.evidence}
    assert wire.summaries == original.summaries
    assert wire.evidence == sorted(original.evidence, key=lambda e: (e.source_id,
        e.locator_start or 0, e.original_evidence_id or e.id, e.text_start, e.id))
    assert [e.source_id for e in wire.evidence] == sorted(e.source_id for e in original.evidence)
    assert gateway.ordered_domain_request(wire) == wire


@pytest.mark.parametrize('case,expected', [('budget', 'node_reading_budget'),
    ('repeated', 'node_repeated_statement'), ('reference', 'node_evidence_reference'),
    ('citations', 'node_citation_budget')])
def test_node_validation_reports_safe_specific_reason(case, expected):
    data = request_data(); result = result_data(data)
    if case == 'budget':
        result['overview']['text'] = '字' * 800
        result['insights'] = [dict(text='额外', citations=[data['evidence'][0]['id']])]
    elif case == 'repeated': result['insights'] = [copy.deepcopy(result['overview'])]
    elif case == 'reference': result['overview']['citations'] = ['unknown']
    else:
        for e in data['evidence']:
            e.update(text='字' * 3000, text_end=3000)
        data['max_cited_characters'] = 3000
    with client(lambda _: provider(result)) as http:
        reply = http.post('/v1/summarize-node', json=data, headers=AUTH)
    assert reply.status_code == 502
    detail = reply.json()['detail']
    assert detail['reason'] == expected and detail['stage'] == 'evidence_validation'
    assert isinstance(detail['budget_diagnostic']['cited_characters'], int)
    assert result['overview']['text'] not in json.dumps(detail)


@pytest.mark.parametrize('reason', ['node_citation_budget', {'private': 'synthetic-secret'}, ['bad']])
def test_worker_failure_diagnostics_do_not_persist_untrusted_messages(reason):
    text = worker.validation_failure_text({'reason': reason, 'message': 'synthetic-secret',
        'fields': ['overview', 'synthetic-secret', {'private': 'synthetic-secret'}]})
    assert 'synthetic-secret' not in text and 'overview' in text
    if reason == 'node_citation_budget': assert '引用原文超出合成上限' in text


def test_long_original_citation_costs_and_bounded_multi_source_selection():
    data = request_data()
    data['max_cited_characters'] = 6000
    first = data['evidence'][0]
    first.update(text='甲' * 3000, text_end=3000)
    second = data['evidence'][1]
    second.update(text='乙' * 1708, text_end=1708)
    third = dict(id=str(uuid4()), source_id=str(uuid4()), revision_id=str(uuid4()),
        text='丙' * 978, text_start=0, text_end=978,
        locator_kind='paragraph', locator_start=0, locator_end=1)
    extra = dict(first, id=str(uuid4()), text_start=3000, text_end=6000)
    data['evidence'].extend([third, extra])
    data['summaries'][0]['citations'].append(extra['id'])
    data['summaries'].append(dict(node_id=str(uuid4()), kind='document', text='合成摘要。',
        source_ids=[third['source_id']], citations=[third['id']]))
    snapshot = DomainSummaryRequest.model_validate(data)
    prompt = gateway.domain_prompt_for(snapshot)
    assert '6000原文字符' in prompt and 'citations并集' in prompt
    for evidence in snapshot.evidence:
        assert json.dumps({'id': evidence.id, 'source_id': evidence.source_id,
            'characters': len(evidence.text)}, ensure_ascii=False) in prompt
        assert evidence.text not in prompt
    result = DomainSummaryResult.model_validate(dict(contract_version=1,
        overview=dict(text='合成资料的概览。', citations=[first['id'], second['id'], third['id']]),
        limitations=[dict(text='合成条件。', citations=[first['id']])]))
    assert validate_domain_summary(snapshot, result) == result
    result.overview.citations.append(extra['id'])
    with pytest.raises(ValueError, match='synthesis budget'):
        validate_domain_summary(snapshot, result)
