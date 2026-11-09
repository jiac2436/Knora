"""Shared execution ownership and duplicate-call boundaries; synthetic only."""
import asyncio
import base64
import hashlib
from io import BytesIO
import json
from uuid import uuid4

import httpx
from PIL import Image
import pytest

from app.ai_contract import AnalysisRequest
from services.ai_gateway.errors import ModelExecutionError
from services.ai_gateway.execution import ModelExecutor
from services.ai_gateway.settings import GatewaySettings
from test_ai_gateway import snapshot, upstream
from test_ai_images import output as image_result, upstream as image_response


def test_cancelled_waiter_does_not_release_capacity_or_repeat_call():
    async def check():
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def provider(request):
            calls.append(request.url); started.set()
            await finish.wait()
            return upstream()
        executor = ModelExecutor(GatewaySettings('synthetic', 'model', 'token'),
            transport=httpx.MockTransport(provider), max_concurrent=1)
        data = AnalysisRequest.model_validate(snapshot())
        first = asyncio.create_task(executor.execute('analyze', data))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(ModelExecutionError) as error:
            await executor.execute('analyze', AnalysisRequest.model_validate(snapshot()))
        assert error.value.status_code == 503
        duplicate = asyncio.create_task(executor.execute('analyze', data))
        finish.set()
        result = await duplicate
        # A consumer cannot overwrite the retained cached result.
        result.result.summary = 'consumer changed result'
        cached = await executor.execute('analyze', data)
        assert cached.result.summary == '合成摘要' and len(calls) == 1
        await executor.aclose()
    asyncio.run(check())


def test_close_marks_pending_call_unknown_and_prevents_new_execution():
    async def check():
        started = asyncio.Event()
        async def provider(request):
            started.set()
            await asyncio.Event().wait()
        executor = ModelExecutor(GatewaySettings('synthetic', 'model', 'token'), transport=httpx.MockTransport(provider))
        first = asyncio.create_task(executor.execute('analyze', AnalysisRequest.model_validate(snapshot())))
        await started.wait()
        await executor.aclose()
        with pytest.raises(ModelExecutionError) as unknown:
            await first
        assert unknown.value.detail['code'] == 'provider_result_unknown'
        with pytest.raises(ModelExecutionError) as closed:
            await executor.execute('analyze', AnalysisRequest.model_validate(snapshot()))
        assert closed.value.detail['code'] == 'executor_unavailable'
    asyncio.run(check())


def test_executor_cannot_reuse_cached_tasks_on_another_event_loop():
    calls = []
    executor = ModelExecutor(GatewaySettings('synthetic', 'model', 'token'),
        transport=httpx.MockTransport(lambda request: calls.append(request.url) or upstream()))
    data = AnalysisRequest.model_validate(snapshot())
    asyncio.run(executor.execute('analyze', data))
    with pytest.raises(ModelExecutionError) as error:
        asyncio.run(executor.execute('analyze', data))
    assert error.value.detail['code'] == 'executor_unavailable' and len(calls) == 1


def test_original_image_bytes_and_citations_survive_shared_executor():
    image = BytesIO(); Image.new('RGB', (80, 60), color='blue').save(image, format='PNG')
    original = image.getvalue()
    data = snapshot(); data['contract_version'] = 3
    image_id = str(uuid4())
    data['evidence'][0]['image_id'] = image_id
    data['images'] = [dict(id=image_id, evidence_id='e1', ordinal=0,
        sha256=hashlib.sha256(original).hexdigest(), mime_type='image/png', size_bytes=len(original),
        width=80, height=60, data_url='data:image/png;base64,' + base64.b64encode(original).decode())]
    calls = []
    def provider(request):
        calls.append(request.url)
        payload = json.loads(request.content)
        assert payload['model'] == 'deepseek-flash'
        blocks = payload['messages'][1]['content']
        actual = next(block['image_url']['url'] for block in blocks if block['type'] == 'image_url')
        assert base64.b64decode(actual.split(',', 1)[1]) == original
        assert 'data_url' not in blocks[0]['text']
        return image_response(image_result('e1'))
    async def check():
        executor = ModelExecutor(GatewaySettings('synthetic', 'text', 'token', vision_model='deepseek-flash'),
            transport=httpx.MockTransport(provider))
        first = await executor.execute_json('analyze', json.dumps(data).encode())
        second = await executor.execute_json('analyze', json.dumps(data).encode())
        assert first == second and first.result.overview.citations == ['e1']
        assert len(calls) == 1
        await executor.aclose()
    asyncio.run(check())


def test_model_configuration_representation_excludes_credentials():
    settings = GatewaySettings('private-provider-key', 'model', 'private-gateway-token')
    assert 'private-provider-key' not in repr(settings)
    assert 'private-gateway-token' not in repr(settings)
