import json
from dataclasses import replace
import httpx
import pytest
from services.ai_gateway import main as gateway
from app.embedded_gateway import gateway_transport
from app import embedded_gateway
from services.ai_gateway.execution import ModelExecutor
from app.ai_contract import AnalysisRequest
from test_ai_gateway import snapshot, upstream


@pytest.fixture
def embedded(monkeypatch):
    values = {'PKA_AI_EMBEDDED':'1', 'PKA_AI_PROVIDER_KIND':'openai-compatible',
              'PKA_AI_PROVIDER_BASE_URL':'https://synthetic.example/v1',
              'PKA_AI_PROVIDER_KEY':'synthetic-key', 'PKA_AI_MODEL':'synthetic-model',
              'PKA_AI_GATEWAY_URL':'http://127.0.0.1/embedded-ai/synthetic-config',
              'PKA_AI_GATEWAY_TOKEN':'synthetic-local-token', 'PKA_AI_VISION_MODEL':''}
    for key, value in values.items(): monkeypatch.setenv(key, value)
    embedded_gateway.close_embedded_runtime()
    yield values
    embedded_gateway.close_embedded_runtime()


def test_embedded_worker_uses_selected_model_and_existing_validation(embedded, monkeypatch):
    from services.worker import ai_analysis
    seen=[]
    def provider(request):
        assert str(request.url)=='https://synthetic.example/v1/chat/completions'
        assert request.headers['authorization']=='Bearer synthetic-key'
        body=json.loads(request.content)
        assert body['model']=='synthetic-model'
        assert 'thinking' not in body and 'reasoning_effort' not in body
        seen.append(body)
        return upstream()
    monkeypatch.setattr(embedded_gateway, 'create_executor',
        lambda configuration: ModelExecutor(configuration, transport=httpx.MockTransport(provider)))
    monkeypatch.setattr(ai_analysis, 'settings', replace(ai_analysis.settings,
        ai_gateway_url=embedded['PKA_AI_GATEWAY_URL'], ai_gateway_token=embedded['PKA_AI_GATEWAY_TOKEN']))
    status,error,response=ai_analysis.call_gateway(AnalysisRequest.model_validate(snapshot()))
    assert status=='completed',error
    assert response.provider=='openai-compatible'
    assert response.model=='synthetic-model'
    assert len(seen)==1


def test_embedded_health_auth_and_no_provider_call(embedded):
    with httpx.Client(transport=gateway_transport()) as client:
        base=embedded['PKA_AI_GATEWAY_URL']
        assert client.post(base+'/v1/analyze', json=snapshot()).status_code==401
        reply=client.get(base+'/health',headers={'Authorization':'Bearer synthetic-local-token'})
        assert reply.status_code==200
        assert 'synthetic-key' not in reply.text
        with pytest.raises(ValueError): client.get('https://unrelated.example/health')


def test_external_transport_unchanged(monkeypatch):
    monkeypatch.delenv('PKA_AI_EMBEDDED',raising=False)
    assert gateway_transport() is None
    transport=httpx.MockTransport(lambda _:httpx.Response(200))
    assert gateway_transport(transport) is transport


def test_generic_vision_name_and_deepseek_compatibility():
    generic=gateway.GatewaySettings('key','text','token',vision_model='custom-vision',provider='openai-compatible')
    assert generic.effective_vision_model=='custom-vision'
    with pytest.raises(ValueError): gateway.GatewaySettings('key','text','token',vision_model='custom-vision')


@pytest.mark.parametrize('kind', ['rag','domain'])
def test_embedded_other_worker_calls(embedded, monkeypatch, kind):
    if kind == 'rag':
        from services.worker import rag_answer as worker
        from app.rag_contract import RAGRequest as Request
        from test_rag_gateway import snapshot as data, output as result, upstream as response
        call = worker.call_answer
    else:
        from services.worker import domain_summary as worker
        from app.domain_contract import DomainSummaryRequest as Request
        from test_domain_summary_contract import request_data as data, result_data as result, provider as response
        call = worker.call_node_gateway
    payload=data()
    seen=[]
    def provider(request):
        body=json.loads(request.content)
        assert body['model']=='synthetic-model'
        assert 'thinking' not in body
        seen.append(body)
        return response(result(payload))
    monkeypatch.setattr(embedded_gateway, 'create_executor',
        lambda configuration: ModelExecutor(configuration, transport=httpx.MockTransport(provider)))
    monkeypatch.setattr(worker, 'settings', replace(worker.settings,
        ai_gateway_url=embedded['PKA_AI_GATEWAY_URL'], ai_gateway_token=embedded['PKA_AI_GATEWAY_TOKEN']))
    status,error,reply=call(Request.model_validate(payload))
    assert status == ('succeeded' if kind == 'rag' else 'completed'), error
    assert reply.model=='synthetic-model'
    assert len(seen)==1


def install_provider(monkeypatch, provider, **limits):
    monkeypatch.setattr(embedded_gateway, 'create_executor',
        lambda configuration: ModelExecutor(configuration, transport=httpx.MockTransport(provider), **limits))


def post(embedded, route, data):
    with httpx.Client(transport=gateway_transport()) as client:
        return client.post(embedded['PKA_AI_GATEWAY_URL'] + '/v1/' + route,
            headers={'Authorization': 'Bearer synthetic-local-token'}, json=data)


@pytest.mark.parametrize('route', ['analyze', 'answer', 'summarize-node'])
def test_repeated_request_across_new_clients_executes_once(embedded, monkeypatch, route):
    import copy
    if route == 'analyze':
        data = snapshot()
        reply = upstream()
    elif route == 'answer':
        from test_rag_gateway import snapshot as request_data, output, upstream as response
        data = request_data(); reply = response(output(data))
    else:
        from test_domain_summary_contract import request_data, result_data, provider
        data = request_data(); reply = provider(result_data(data))
    calls = []
    install_provider(monkeypatch, lambda request: calls.append(request.url) or reply)
    first = post(embedded, route, data)
    assert first.status_code == 200, first.text
    assert post(embedded, route, data).json() == first.json()
    changed = copy.deepcopy(data)
    changed['context' if route == 'analyze' else 'question' if route == 'answer' else 'title'] = (
        {'note': 'changed'} if route == 'analyze' else 'changed')
    assert post(embedded, route, changed).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['timeout', 'http_error', 'invalid_result'])
def test_failed_requests_are_cached_across_clients(embedded, monkeypatch, failure):
    calls = []
    def provider(request):
        calls.append(request.url)
        if failure == 'timeout':
            raise httpx.ReadTimeout('synthetic-provider-secret')
        if failure == 'http_error':
            return httpx.Response(401, text='synthetic-provider-secret')
        return httpx.Response(200, content=b'synthetic-provider-secret')
    install_provider(monkeypatch, provider)
    data = snapshot()
    first, second = post(embedded, 'analyze', data), post(embedded, 'analyze', data)
    assert first.status_code == (504 if failure == 'timeout' else 502)
    assert first.json() == second.json()
    assert 'synthetic-provider-secret' not in first.text
    assert len(calls) == 1


@pytest.mark.parametrize('field,value', [('PKA_AI_MODEL', 'other-model'),
    ('PKA_AI_PROVIDER_KEY', 'other-key'), ('PKA_AI_PROVIDER_BASE_URL', 'https://other.example/v1'),
    ('PKA_AI_GATEWAY_TOKEN', 'other-token'), ('PKA_AI_GATEWAY_URL', 'http://127.0.0.1/embedded-ai/other')])
def test_live_runtime_cannot_silently_change_configuration(embedded, monkeypatch, field, value):
    calls = []
    install_provider(monkeypatch, lambda request: calls.append(request.url) or upstream())
    assert post(embedded, 'analyze', snapshot()).status_code == 200
    monkeypatch.setenv(field, value)
    if field == 'PKA_AI_GATEWAY_TOKEN':
        with httpx.Client(transport=gateway_transport()) as client:
            response = client.post(embedded['PKA_AI_GATEWAY_URL'] + '/v1/analyze',
                headers={'Authorization': 'Bearer other-token'}, json=snapshot())
    else:
        response = post({**embedded, field: value}, 'analyze', snapshot())
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'embedded_configuration_changed'
    assert len(calls) == 1


def test_capacity_survives_new_client_creation(embedded, monkeypatch):
    calls = []
    install_provider(monkeypatch, lambda request: calls.append(request.url) or upstream(), max_requests=1)
    data = snapshot()
    assert post(embedded, 'analyze', data).status_code == 200
    assert post(embedded, 'analyze', data).status_code == 200
    assert post(embedded, 'analyze', snapshot()).status_code == 503
    assert len(calls) == 1


def test_concurrent_waiters_share_one_runtime_and_capacity(embedded, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    import threading
    started, finish = threading.Event(), threading.Event()
    calls = []
    async def provider(request):
        calls.append(request.url); started.set()
        while not finish.is_set():
            await asyncio.sleep(0.005)
        return upstream()
    install_provider(monkeypatch, provider, max_concurrent=1)
    data = snapshot()
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(post, embedded, 'analyze', data)
        try:
            assert started.wait(3)
            duplicate = pool.submit(post, embedded, 'analyze', data)
            assert post(embedded, 'analyze', snapshot()).status_code == 503
        finally:
            finish.set()
        assert first.result(timeout=3).json() == duplicate.result(timeout=3).json()
    assert len(calls) == 1


def test_worker_runtime_closes_its_loop_and_thread(embedded, monkeypatch):
    install_provider(monkeypatch, lambda request: upstream())
    assert post(embedded, 'analyze', snapshot()).status_code == 200
    runtime = embedded_gateway._runtime
    embedded_gateway.close_embedded_runtime()
    assert not runtime.thread.is_alive() and runtime.loop.is_closed()
    assert embedded_gateway._runtime is None
