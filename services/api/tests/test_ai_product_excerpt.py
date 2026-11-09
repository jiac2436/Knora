"""Synthesis citations preserve real text, later facts, offsets and wire budgets."""
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest

from app.ai_long_analysis import MAX_WIRE_BYTES, dump, plan_hash, synthesis_request


def planned(texts, version=3, derived=False):
    evidence = []
    for number, text in enumerate(texts):
        item = {'id': f'e-{number}', 'text': text, 'locator_kind': 'paragraph',
                'locator_start': number, 'locator_end': number + 1}
        if derived:
            item.update(original_evidence_id=f'original-{number}', text_start=24000, text_end=24000 + len(text))
        evidence.append(item)
    snapshot = {'request_id': str(uuid4()), 'contract_version': version, 'phase': 'single',
        'kind': 'analyze', 'source_id': str(uuid4()), 'revision_id': str(uuid4()),
        'context': {}, 'evidence': evidence, 'analysis_plan': {'coverage': 'full'}}
    return snapshot


def segments(snapshot, facts):
    return [SimpleNamespace(phase='segment', status='completed', response_json=json.dumps({
        'result': {'contract_version': snapshot['contract_version'],
            'overview': {'text': '章节概述', 'citations': [item['id']]},
            'summary': fact, 'summary_citations': [item['id']]},
        'provider': 'synthetic', 'model': 'synthetic', 'prompt_version': 'segment-test',
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}))
        for item, fact in zip(snapshot['evidence'], facts, strict=True)]


def test_late_fact_is_preserved_when_full_cited_evidence_fits():
    fact = '索引切换前必须完成数据库版本校验。'
    texts = ['背景信息。' * 3000 + fact, '其他背景。' * 3000]
    snapshot = planned(texts)
    original_hash = plan_hash(snapshot)
    result = synthesis_request(snapshot, segments(snapshot, [fact, '其他背景']), str(uuid4()))
    assert result['evidence'][0]['text'] == texts[0]
    assert fact in result['evidence'][0]['text']
    assert len(result['evidence'][0]['text']) > 500
    assert 'synthesis_evidence' not in result['context']
    assert plan_hash(snapshot) == original_hash


def test_over_budget_uses_late_facts_and_exact_original_offsets():
    facts = [f'章节{number}的切换代号为 VERIFIED-{number}-CEDAR。' for number in range(4)]
    texts = ['背景说明。' * 3000 + fact for fact in facts]
    snapshot = planned(texts, derived=True)
    original_hash = plan_hash(snapshot)
    result = synthesis_request(snapshot, segments(snapshot, facts), str(uuid4()))
    assert sum(len(item['text']) for item in result['evidence']) <= 48000
    assert len(dump(result).encode('utf-8')) <= MAX_WIRE_BYTES
    for index, item in enumerate(result['evidence']):
        assert facts[index] in item['text']
        start, end = item['text_start'] - 24000, item['text_end'] - 24000
        assert item['text'] == texts[index][start:end]
        assert item['original_evidence_id'] == f'original-{index}'
        assert item['text_end'] - item['text_start'] == len(item['text'])
    assert result['context']['synthesis_evidence']['coverage'] == 'partial'
    assert snapshot['analysis_plan']['synthesis_evidence_coverage'] == 'partial'
    assert snapshot['warnings']
    assert snapshot['evidence'] == result['evidence']
    assert plan_hash(snapshot) == original_hash
    assert 'original_plan_evidence' not in result and 'original_plan_metadata' not in result
    snapshot['context']['notes'] = '已修改的上下文'
    assert plan_hash(snapshot) != original_hash


def test_v2_remains_prefix_compatible():
    fact = '正文后部事实。'
    snapshot = planned(['背景。' * 1000 + fact], version=2)
    result = synthesis_request(snapshot, segments(snapshot, [fact]), str(uuid4()))
    assert len(result['evidence'][0]['text']) == 500
    assert fact not in result['evidence'][0]['text']
    assert 'warnings' not in snapshot


def test_small_chunk_kept_whole_while_large_chunk_uses_remaining_budget():
    fact = '末尾必须核验原文。'
    texts = ['短说明。', '背景说明。' * 10000 + fact]
    snapshot = planned(texts)
    result = synthesis_request(snapshot, segments(snapshot, texts[:1] + [fact]), str(uuid4()))
    assert result['evidence'][0]['text'] == texts[0]
    assert fact in result['evidence'][1]['text']
    assert sum(len(item['text']) for item in result['evidence']) <= 48000


def test_missing_cited_original_fails_without_fabrication():
    snapshot = planned(['正文。'])
    steps = segments(snapshot, ['正文。'])
    snapshot['evidence'] = []
    with pytest.raises(ValueError, match='Missing synthesis original evidence'):
        synthesis_request(snapshot, steps, str(uuid4()))


def test_utf8_and_large_context_are_budgeted_before_original_windows():
    fact = '后部事实必须可核验。'
    texts = ['😀' * 20000 + fact, '😀' * 20000 + fact]
    snapshot = planned(texts)
    snapshot['context'] = {'notes': 'x' * 400000}
    result = synthesis_request(snapshot, segments(snapshot, [fact, fact]), str(uuid4()))
    assert len(dump(result).encode('utf-8')) <= MAX_WIRE_BYTES
    assert all(fact in item['text'] for item in result['evidence'])
    assert result['context']['synthesis_evidence']['coverage'] == 'partial'


def test_eighty_cited_chunks_stay_bounded_and_keep_late_anchors():
    texts = ['背景。' * 500 + f'专项代号 ANCHOR-{number}-CEDAR。' for number in range(80)]
    snapshot = planned(texts)
    steps = []
    for section in range(8):
        identifiers = [item['id'] for item in snapshot['evidence'][section * 10:(section + 1) * 10]]
        facts = [f'专项代号 ANCHOR-{number}-CEDAR。' for number in range(section * 10, (section + 1) * 10)]
        steps.append(SimpleNamespace(phase='segment', status='completed', response_json=json.dumps({
            'result': {'contract_version': 3, 'overview': {'text': '章节代号', 'citations': identifiers[:5]},
                'summary': ' '.join(facts[:5]), 'summary_citations': identifiers[:5],
                'insights': [{'text': ' '.join(facts[5:]), 'citations': identifiers[5:]}]},
            'provider': 'synthetic', 'model': 'synthetic', 'prompt_version': 'segment-test',
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})))
    result = synthesis_request(snapshot, steps, str(uuid4()))
    assert len(result['evidence']) == 80
    assert sum(len(item['text']) for item in result['evidence']) <= 48000
    assert len(dump(result).encode('utf-8')) <= MAX_WIRE_BYTES
    for number, item in enumerate(result['evidence']):
        assert f'ANCHOR-{number}-CEDAR' in item['text']
        assert texts[number][item['text_start']:item['text_end']] == item['text']
