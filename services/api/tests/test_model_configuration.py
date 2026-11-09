"""Synthetic private startup and device configuration; no Keychain/model access."""
from dataclasses import replace
from io import BytesIO
import json
from uuid import uuid4

import httpx
import pytest

from app import model_configuration as configuration, embedded_gateway
from app.ai_contract import AnalysisRequest
from services.ai_gateway.execution import ModelExecutor
from test_ai_gateway import snapshot, upstream


def profile(**values):
    return configuration.ModelProfile(profile_id=uuid4(), config_revision=1,
        mode='direct', status='ready', credential_ref='synthetic.model',
        credential_generation=uuid4(), provider='openai-compatible', model='synthetic-model',
        base_url='https://synthetic.example/v1', **values)


def startup(value=None):
    return configuration.ModelStartup(profile=value or profile(), provider_key='synthetic-provider-key',
                                      gateway_token='synthetic-internal-token')


def test_versioned_bounded_messages_and_legacy_owner_token():
    message = configuration.HostStartup(version=1, owner_credential='a' * 48, ai=startup())
    wire = configuration.encode_message(message)
    assert configuration.read_message(BytesIO(wire)) == message
    assert 'synthetic-provider-key' not in repr(message)
    assert 'synthetic-internal-token' not in repr(message)
    assert 'a' * 48 not in repr(message)
    legacy = configuration.read_message(BytesIO(b'a' * 48 + b'\n'))
    assert legacy.owner_credential == 'a' * 48 and legacy.ai is None
    for data in (wire.replace(b'"version":1', b'"version":9'), wire[:-1],
                 b'x' * (configuration.MAX_BOOTSTRAP_BYTES + 1)):
        with pytest.raises(ValueError) as error:
            configuration.read_message(BytesIO(data))
        assert str(error.value) in {'私有启动消息格式或模型配置无效', '私有启动消息缺失或超过大小限制'}
        assert 'secret' not in str(error.value)
    with pytest.raises(ValueError):
        configuration.read_message(BytesIO(wire), worker=True)
    for data in (wire.replace(b'https://synthetic.example/v1', b'http://synthetic.example/v1'),
                 wire.replace(b'synthetic-provider-key', b'bad\\nsecret')):
        invalid = configuration.read_message(BytesIO(data))
        assert invalid.ai.profile.status == 'invalid'
        assert not invalid.ai.provider_key and not invalid.ai.gateway_token


@pytest.mark.parametrize('state', ['missing', 'denied', 'unavailable', 'invalid', 'disabled'])
def test_unavailable_model_does_not_require_provider_key_or_disable_owner(monkeypatch, state):
    current = replace(configuration.settings)
    monkeypatch.setattr(configuration, 'settings', current)
    ai = configuration.ModelStartup(profile=profile().model_copy(update={'status': state}))
    message = configuration.HostStartup(version=1, owner_credential='a' * 48, ai=ai)
    parsed = configuration.read_message(BytesIO(configuration.encode_message(message)))
    configuration.install_startup(parsed.ai)
    assert current.ai_profile['status'] == state
    assert current.ai_gateway_url == current.ai_gateway_token == ''
    assert parsed.owner_credential == 'a' * 48


def test_only_worker_installs_model_secret_and_reuses_executor(monkeypatch):
    current = replace(configuration.settings)
    monkeypatch.setattr(configuration, 'settings', current)
    monkeypatch.setattr(embedded_gateway, '_startup_configuration', None)
    ai = startup()
    calls = []
    monkeypatch.setattr(embedded_gateway, 'create_executor', lambda value:
        ModelExecutor(value, transport=httpx.MockTransport(lambda request: calls.append(request) or upstream())))
    embedded_gateway.close_embedded_runtime()
    try:
        configuration.install_startup(ai)
        assert embedded_gateway._startup_configuration is None
        assert 'synthetic-provider-key' not in repr(current)
        configuration.install_startup(ai, worker=True)
        headers = {'Authorization': 'Bearer synthetic-internal-token'}
        body = AnalysisRequest.model_validate(snapshot()).model_dump()
        for _ in range(2):
            with httpx.Client(transport=embedded_gateway.gateway_transport()) as client:
                response = client.post(ai.profile.binding + '/v1/analyze', headers=headers, json=body)
                assert response.status_code == 200, response.text
        assert len(calls) == 1
        assert calls[0].headers['authorization'] == 'Bearer synthetic-provider-key'
        frozen = configuration.frozen_model_binding(current)
        assert configuration.model_binding_matches(frozen, current)
        assert not configuration.model_binding_matches({}, current)
        changed = replace(current, ai_profile={**current.ai_profile, 'config_revision': 2})
        assert not configuration.model_binding_matches(frozen, changed)
        assert 'synthetic-provider-key' not in json.dumps(frozen)
    finally:
        embedded_gateway.close_embedded_runtime()


def test_worker_environment_drops_secrets_and_unrelated_tool_configuration():
    environment = dict(HOME='/synthetic', PATH='/bin', PKA_ENVIRONMENT='local',
        PKA_AI_PROVIDER_KEY='synthetic-secret', PKA_AI_GATEWAY_TOKEN='synthetic-token',
        OPENAI_API_KEY='another-secret', PYTHONPATH='/wrong', PKA_WORKER_ROOT='/worker',
        PKA_DATA_DIR='/synthetic/data', PKA_SEMANTIC_ENABLED='0')
    safe = configuration.worker_environment(environment)
    assert set(safe) == {'HOME', 'PATH', 'PKA_ENVIRONMENT', 'PKA_DATA_DIR', 'PKA_SEMANTIC_ENABLED'}


@pytest.mark.parametrize('mode', ['direct', 'external'])
def test_configuration_requires_matching_credential_channel(mode):
    value = startup()
    if mode == 'external':
        value = value.model_copy(update={'profile': value.profile.model_copy(update={
            'mode': 'external', 'external_url': 'https://gateway.example'})})
    else:
        value = value.model_copy(update={'provider_key': ''})
    with pytest.raises(ValueError):
        value.validate_connection()
