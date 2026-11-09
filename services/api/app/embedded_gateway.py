"""Worker-owned model executor; compatibility transport without an HTTP server."""
import atexit
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import hmac
import os
import threading
from urllib.parse import urlsplit

import httpx

from services.ai_gateway.errors import ModelExecutionError
from services.ai_gateway.execution import ModelExecutor
from services.ai_gateway.prompts import SUMMARY_PROMPT_VERSION
from services.ai_gateway.settings import GatewaySettings


def create_executor(configuration):
    return ModelExecutor(configuration)


class EmbeddedModelRuntime:
    def __init__(self, configuration, binding):
        self.configuration = configuration
        self.binding = binding
        self.executor = create_executor(configuration)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='pka-model-executor', daemon=True)
        self.thread.start()

    def execute(self, route, body):
        future = asyncio.run_coroutine_threadsafe(self.executor.execute_json(route, body), self.loop)
        try:
            return future.result(timeout=self.configuration.timeout_seconds + 5)
        except FutureTimeout:
            # Do not cancel a provider request whose outcome is unknown.
            raise ModelExecutionError(504, detail={'code': 'provider_result_unknown',
                'message': '模型执行等待超时，结果未知；当前请求不会自动重发。'}) from None

    def close(self):
        if not self.thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(self.executor.aclose(), self.loop)
        try:
            future.result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                self.loop.close()


_runtime = None
_runtime_lock = threading.Lock()
_startup_configuration = None


def configure_embedded(configuration, binding):
    """Install the private Worker bootstrap before accepting tasks."""
    global _startup_configuration
    with _runtime_lock:
        if _runtime is not None:
            raise ValueError('Cannot replace an active model executor')
        _startup_configuration = (configuration, binding)


def _owned_runtime(configuration, binding):
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = EmbeddedModelRuntime(configuration, binding)
        elif _runtime.configuration != configuration or _runtime.binding != binding:
            raise ModelExecutionError(409, detail={'code': 'embedded_configuration_changed',
                'message': '模型配置与当前执行服务不一致，请等待任务结束后重新应用配置。'})
        return _runtime


def close_embedded_runtime():
    global _runtime
    with _runtime_lock:
        previous, _runtime = _runtime, None
    if previous is not None:
        previous.close()


atexit.register(close_embedded_runtime)


def gateway_transport(override=None):
    if override is not None:
        return override
    if _startup_configuration is not None:
        configuration, base = _startup_configuration
    elif os.environ.get('PKA_AI_EMBEDDED') == '1':
        # External development compatibility; the owned desktop uses a pipe.
        configuration = GatewaySettings.from_environment()
        base = os.environ.get('PKA_AI_GATEWAY_URL', '').rstrip('/')
    else:
        return None
    parsed = urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.path.startswith('/embedded-ai/') or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError('Invalid embedded gateway binding')

    def handle(request):
        if not str(request.url).startswith(base + '/'):
            raise ValueError('Unexpected embedded gateway target')
        path = str(request.url)[len(base):]
        if request.method == 'GET' and path == '/health':
            return httpx.Response(200, json={'ready': True, 'node_contract_versions': [1, 2],
                'summary_prompt_version': SUMMARY_PROMPT_VERSION,
                'analysis_image_support': bool(configuration.effective_vision_model)})
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(),
                ('Bearer ' + configuration.gateway_token).encode()):
            return httpx.Response(401, json={'detail': {'code': 'unauthorized', 'message': 'AI 网关访问凭证无效。'}})
        if request.method != 'POST' or path not in ('/v1/analyze', '/v1/answer', '/v1/summarize-node'):
            return httpx.Response(404, json={'detail': {'code': 'unknown_model_route'}})
        try:
            runtime = _owned_runtime(configuration, base)
            result = runtime.execute(path.removeprefix('/v1/'), request.read())
            return httpx.Response(200, content=result.model_dump_json(), headers={'Content-Type': 'application/json'})
        except ModelExecutionError as error:
            return httpx.Response(error.status_code, json={'detail': error.detail})

    return httpx.MockTransport(handle)
