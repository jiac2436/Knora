import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('quality_acceptance',
    Path(__file__).resolve().parents[1] / 'verify_domain_deepseek_quality.py')
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def test_remaining_authorization_accepts_three_nodes():
    quality.check_build_budget({'calls': 3, 'input_token_reservation': 120723}, 5, 47739)


@pytest.mark.parametrize('preview,calls,reserved', [
    ({'calls': 8, 'input_token_reservation': 1000}, 5, 0),
    ({'calls': 4, 'input_token_reservation': 1000}, 4, 37739),
    ({'calls': 1, 'input_token_reservation': 340000}, 4, 37739),
])
def test_remaining_authorization_rejects_overflow(preview, calls, reserved):
    with pytest.raises(ValueError):
        quality.check_build_budget(preview, calls, reserved)


def fixture():
    previous = {'input': {'evidence': [{'id': 'old', 'text': '不重发付费请求'}]},
        'response': {'prompt_version': 'original', 'usage': {'prompt_tokens': 10},
            'result': {'overview': {'text': '不重发付费请求', 'citations': ['old']},
                'tag_suggestions': [{'name': '失败处理', 'dimension': 'topic', 'tag_id': 'old-tag',
                    'citations': ['old']}]}}}
    body = {'evidence': [{'id': 'new', 'text': '不重发付费请求'}],
        'tags': [{'id': 'new-tag', 'name': '失败处理', 'dimension': 'topic'}]}
    return previous, body


def test_replay_preserves_facts_and_original_report():
    previous, body = fixture()
    original = copy.deepcopy(previous)
    response = quality.replay_response(previous, body)
    assert response['result']['overview'] == {'text': '不重发付费请求', 'citations': ['new']}
    assert response['result']['tag_suggestions'][0]['tag_id'] == 'new-tag'
    assert response['prompt_version'] == 'original'
    assert previous == original


def test_replay_refuses_changed_original():
    previous, body = fixture()
    body['evidence'][0]['text'] = '重新发送付费请求'
    with pytest.raises(ValueError):
        quality.replay_response(previous, body)
