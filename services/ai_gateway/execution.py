"""One event-loop-owned execution service, independent of an HTTP server."""
import asyncio
import hashlib
import hmac
import json

from app.ai_contract import AnalysisRequest
from app.domain_contract import DomainSummaryRequest
from app.rag_contract import RAGRequest

from .errors import ModelExecutionError
from .provider import _provider_call
from .settings import GatewaySettings

MAX_REQUEST_BYTES = 512 * 1024
MAX_NODE_REQUEST_BYTES = 2 * 1024 * 1024
MAX_REQUESTS = 256
MAX_CONCURRENT_REQUESTS = 4
CONTRACTS = {'analyze': AnalysisRequest, 'answer': RAGRequest, 'summarize-node': DomainSummaryRequest}


def body_limit(route: str) -> int:
    from app.ai_images import MAX_VISUAL_WIRE_BYTES
    if route not in CONTRACTS:
        raise ModelExecutionError(404, detail={'code': 'unknown_model_route'})
    return {'analyze': MAX_VISUAL_WIRE_BYTES, 'summarize-node': MAX_NODE_REQUEST_BYTES}.get(route, MAX_REQUEST_BYTES)


def parse_request(route: str, body: bytes | bytearray):
    if len(body) > body_limit(route):
        raise ModelExecutionError(413, detail={'code': 'request_too_large', 'message': '分析资料超出请求大小限制。'})
    try:
        snapshot = CONTRACTS[route].model_validate_json(body)
    except ValueError:
        if len(body) > MAX_REQUEST_BYTES:
            raise ModelExecutionError(413, detail={'code': 'request_too_large', 'message': '资料超出请求大小限制。'}) from None
        raise ModelExecutionError(422, detail={'code': 'invalid_request', 'message': '分析请求格式无效或证据超出限制。'}) from None
    visual = isinstance(snapshot, AnalysisRequest) and any(image.data_url for image in snapshot.images)
    if len(body) > MAX_REQUEST_BYTES and not (visual or isinstance(snapshot, DomainSummaryRequest) and snapshot.contract_version == 2):
        raise ModelExecutionError(413, detail={'code': 'request_too_large', 'message': '资料超出请求大小限制。'})
    return snapshot


class ModelExecutor:
    def __init__(self, settings: GatewaySettings, *, transport=None, max_requests=None, max_concurrent=None):
        self.settings = settings
        self.transport = transport
        self.max_requests = MAX_REQUESTS if max_requests is None else max_requests
        self.max_concurrent = MAX_CONCURRENT_REQUESTS if max_concurrent is None else max_concurrent
        self._requests = {}
        self._active = 0
        self._loop = None
        self._closed = False

    def _check_loop(self):
        current = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = current
        if self._loop is not current or self._closed:
            raise ModelExecutionError(503, detail={'code': 'executor_unavailable', 'message': '模型执行服务已停止或运行环境不一致。'})

    async def execute_json(self, route: str, body: bytes):
        return await self.execute(route, parse_request(route, body))

    async def execute(self, route: str, snapshot):
        self._check_loop()
        if route not in CONTRACTS or not isinstance(snapshot, CONTRACTS[route]):
            raise ModelExecutionError(422, detail={'code': 'invalid_request', 'message': '模型任务类型不匹配。'})
        canonical = json.dumps(snapshot.model_dump(), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        key = (route, snapshot.request_id)
        cached = self._requests.get(key)
        if cached:
            if not hmac.compare_digest(cached[0], fingerprint):
                raise ModelExecutionError(409, detail={'code': 'request_id_conflict', 'message': '相同请求编号对应的资料快照不同。'})
            task = cached[1]
        else:
            if len(self._requests) >= self.max_requests or self._active >= self.max_concurrent:
                raise ModelExecutionError(503, detail={'code': 'gateway_capacity', 'message': '模型处理容量已满，请稍后重试或检查服务状态。'})
            self._active += 1
            task = asyncio.create_task(self._execute(snapshot.model_copy(deep=True)))
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self._requests[key] = (fingerprint, task)
        # A cancelled waiter does not cancel or free capacity for a paid call.
        result = await asyncio.shield(task)
        return result.model_copy(deep=True)

    async def _execute(self, snapshot):
        failure = None
        try:
            return await asyncio.wait_for(_provider_call(self.settings, snapshot, self.transport), self.settings.timeout_seconds)
        except ModelExecutionError as error:
            failure = ModelExecutionError(error.status_code, detail=error.detail)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            failure = ModelExecutionError(504, detail={'code': 'provider_result_unknown', 'message': '大模型请求超时或中断，可能已产生用量；当前请求不会自动重发。'})
        except Exception:
            failure = ModelExecutionError(502, detail={'code': 'provider_result_unknown', 'message': '大模型执行异常，结果未知；当前请求不会自动重发。'})
        finally:
            if isinstance(snapshot, AnalysisRequest):
                for image in snapshot.images:
                    image.data_url = None
            self._active -= 1
        # Cache only this safe failure, not provider frames or image payloads.
        raise failure

    async def aclose(self):
        self._closed = True
        pending = [task for _, task in self._requests.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
