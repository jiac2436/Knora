"""Compact knowledge output budgets and legacy compatibility, without paid calls."""
import copy
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai_contract import AnalysisRequest, AnalysisResult, validate_result
from services.ai_gateway.main import GatewaySettings, create_app, prompt_for, normalize_compact_json


def request_data(text='知识管理资料。' * 100, phase='single'):
    return dict(contract_version=3, request_id=str(uuid4()), kind='analyze', source_id='s', revision_id='r',
                phase=phase, evidence=[dict(id='e', text=text, locator_kind='paragraph', locator_start=0, locator_end=1)])


def result_data():
    return dict(contract_version=3, overview=dict(text='介绍个人知识库的管理方法。', citations=['e']),
                summary='通过规范标签和原文检索组织个人资料。', summary_citations=['e'],
                insights=[dict(text='标签帮助筛选资料，原文支撑事实回答。', citations=['e'])],
                tag_suggestions=[dict(tag_id=None, name='知识管理', dimension='topic', reason='原文主题', citations=['e'])])


def test_new_and_legacy_contracts_do_not_change_each_other():
    validate_result(AnalysisRequest.model_validate(request_data()), AnalysisResult.model_validate(result_data()))
    legacy = dict(result_data(), contract_version=2, summary='长' * 1000)
    validate_result(AnalysisRequest.model_validate(dict(request_data(), contract_version=2)), AnalysisResult.model_validate(legacy))
    with pytest.raises(ValueError, match='only supports'):
        AnalysisRequest.model_validate(dict(request_data(), kind='organize'))


@pytest.mark.parametrize('case', ['summary', 'overview', 'reading_total', 'insight_count', 'tag_count', 'organization', 'duplicate', 'short_note', 'tag_reason', 'reference'])
def test_reject_expansion_and_unsupported_output(case):
    request, result = request_data(), result_data()
    if case == 'summary': result['summary'] = '字' * 301
    elif case == 'overview': result['overview']['text'] = '字' * 101
    elif case == 'reading_total':
        result['overview']['text'] = '概' * 100
        result['insights'] = [dict(text=c * 100, citations=['e']) for c in '甲乙丙']
    elif case == 'insight_count': result['insights'] *= 4
    elif case == 'tag_count':
        result['tag_suggestions'] = [dict(result['tag_suggestions'][0], name=f'主题{i}') for i in range(6)]
    elif case == 'organization':
        result['organizing_suggestion'] = dict(domain_id=None, project_ids=[], reason='分类', citations=['e'])
    elif case == 'duplicate': result['insights'][0]['text'] = result['overview']['text']
    elif case == 'short_note': request['evidence'][0]['text'] = '短笔记'
    elif case == 'tag_reason': result['tag_suggestions'][0]['reason'] = '理' * 161
    else: result['overview']['citations'] = ['invented']
    with pytest.raises(ValueError):
        validate_result(AnalysisRequest.model_validate(request), AnalysisResult.model_validate(result))


def test_tiny_note_can_have_only_overview_and_no_tags():
    output = dict(contract_version=3, overview=dict(text='记录姓名', citations=['e']),
                  summary='姓名记录', summary_citations=['e'], insights=[], tag_suggestions=[])
    validate_result(AnalysisRequest.model_validate(request_data('我的名字叫杰克。')), AnalysisResult.model_validate(output))


def test_segments_retain_internal_coverage_and_final_english_has_separate_budget():
    data = result_data()
    data['summary'] = '分' * 1000
    data['tag_suggestions'] = []
    validate_result(AnalysisRequest.model_validate(request_data(phase='segment')), AnalysisResult.model_validate(data))
    data = dict(contract_version=3, overview=dict(text='Overview ' * 10, citations=['e']),
                summary='Summary ' * 60, summary_citations=['e'], insights=[dict(text='Evidence supports retrieval.', citations=['e'])])
    validate_result(AnalysisRequest.model_validate(request_data('English document. ' * 100)), AnalysisResult.model_validate(data))


def test_gateway_compact_prompt_and_token_budget_with_grounded_result():
    seen = []
    output = result_data()
    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(output)))],
                                            usage=dict(prompt_tokens=10, completion_tokens=20)))
    app = create_app(GatewaySettings(provider_key='synthetic', model='test-model', gateway_token='gateway'),
                     transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post('/v1/analyze', json=request_data(), headers={'Authorization': 'Bearer gateway'})
        assert response.status_code == 200, response.text
        assert response.json()['prompt_version'] == 'compact-knowledge-v3.1'
    assert seen[0]['max_tokens'] == 2048
    assert seen[0]['thinking'] == {'type': 'disabled'}
    assert '不建议调整领域或项目' in seen[0]['messages'][0]['content']
    assert '最多5个' in seen[0]['messages'][0]['content']
    assert 'summary 必须是字符串' in seen[0]['messages'][0]['content']
    assert 'summary_citations 是与 summary 并列的必填字符串数组' in seen[0]['messages'][0]['content']
    prompt, version = prompt_for(AnalysisRequest.model_validate(request_data(phase='segment')))
    assert '"contract_version":3' in prompt and '最多2000字' in prompt
    assert version.endswith('-segment')


def test_incomplete_output_exposes_stage_without_provider_content():
    def upstream(_):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length',
            'message': {'content': 'synthetic-private-output'}}]})
    app = create_app(GatewaySettings(provider_key='synthetic', model='test-model', gateway_token='gateway'),
                     transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post('/v1/analyze', json=request_data(), headers={'Authorization': 'Bearer gateway'})
        assert response.status_code == 502
        assert response.json()['detail']['stage'] == 'finish_reason'
        assert 'synthetic-private-output' not in response.text


def test_short_note_prompt_states_actual_budget_and_keeps_single_version():
    prompt, version = prompt_for(AnalysisRequest.model_validate(request_data('短笔记正文')))
    assert '分别最多5字符' in prompt
    assert 'insights必须为[]' in prompt
    assert version == 'compact-knowledge-v3.1'


@pytest.mark.parametrize('structural', [False, True])
def test_output_diagnostic_exposes_only_safe_reason_and_field_names(structural):
    output = result_data()
    if structural:
        output['summary'] = {'private': 'synthetic-private-output'}
    else:
        output['summary'] = '字' * 301
    def upstream(_):
        return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(output)))],
                                            usage=dict(prompt_tokens=10, completion_tokens=20)))
    app = create_app(GatewaySettings(provider_key='synthetic', model='test-model', gateway_token='gateway'),
                     transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post('/v1/analyze', json=request_data(), headers={'Authorization': 'Bearer gateway'})
    assert response.status_code == 502
    detail = response.json()['detail']
    assert detail['stage'] == ('output_structure' if structural else 'evidence_validation')
    assert detail['fields'] == (['summary'] if structural else [])
    assert detail['reason'] == ('invalid_output' if structural else 'summary_budget')
    assert detail['validation_types'] == (['string_type'] if structural else [])
    assert 'synthetic-private-output' not in response.text


def test_invalid_json_diagnostic_does_not_expose_raw_output():
    def upstream(_):
        return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop',
            message=dict(content='{"summary": synthetic-private-output'))]))
    app = create_app(GatewaySettings(provider_key='synthetic', model='test-model', gateway_token='gateway'),
                     transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post('/v1/analyze', json=request_data(), headers={'Authorization': 'Bearer gateway'})
    assert response.status_code == 502
    assert response.json()['detail']['reason'] == 'invalid_json'
    assert response.json()['detail']['validation_types'] == ['json_invalid']
    assert response.json()['detail']['json_diagnostic'] == {
        'characters': len('{"summary": synthetic-private-output'), 'starts_with_object': True,
        'markdown_fence': False, 'empty': False, 'line': 1, 'column': 13, 'category': 'expected_value'}
    assert 'synthetic-private-output' not in response.text


def test_compact_json_normalization_keeps_all_disjoint_fields_and_summary_evidence():
    data = result_data()
    data['summary'] = dict(text=data['summary'], citations=data.pop('summary_citations'))
    raw = '\n'.join(json.dumps({key: value}) for key, value in data.items())
    normalized = normalize_compact_json(raw)
    assert normalized == result_data()
    validate_result(AnalysisRequest.model_validate(request_data()), AnalysisResult.model_validate(normalized))


def test_compact_json_normalization_moves_only_premature_outer_boundary():
    data = result_data()
    early = {key: data[key] for key in ['contract_version', 'overview', 'summary', 'summary_citations']}
    remainder = {key: data[key] for key in data if key not in early}
    raw = json.dumps(early) + ',' + json.dumps(remainder)[1:]
    assert normalize_compact_json(raw) == data
    with pytest.raises(ValueError):
        normalize_compact_json(json.dumps(early) + ',"summary":"conflicting"}')
    with pytest.raises(ValueError):
        normalize_compact_json(raw[:-1])


@pytest.mark.parametrize('raw', ['{"summary":"a"}{"summary":"b"}',
    '{"summary":"a","summary":"a"}', '{"summary":"a"} explanatory text',
    '{"summary":{"text":"a","citations":["e"]},"summary_citations":["other"]}',
    '{"unknown":"a"}', '[]'])
def test_compact_json_normalization_rejects_ambiguous_or_incomplete_values(raw):
    with pytest.raises(ValueError):
        normalize_compact_json(raw)
