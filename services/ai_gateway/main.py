"""Optional HTTP adapter for the shared in-process model execution service."""
from contextlib import asynccontextmanager
import hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx

from app.ai_contract import GatewayResponse
from app.rag_contract import RAGResponse
from app.domain_contract import DomainSummaryResponse
from .errors import ModelExecutionError
from .execution import (ModelExecutor, body_limit, MAX_REQUEST_BYTES, MAX_NODE_REQUEST_BYTES,
    MAX_REQUESTS, MAX_CONCURRENT_REQUESTS)
from .settings import GatewaySettings
# Keep existing imports compatible while implementations live in shared modules.
from .provider import MAX_RESPONSE_BYTES, CompactJSONError, normalize_compact_json, ordered_domain_request
from .prompts import (PROMPT_VERSION, SYSTEM_PROMPT, PROMPT_VERSION_V2, PROMPT_VERSION_V3,
    SYSTEM_PROMPT_V2, SEGMENT_PROMPT, SYSTEM_PROMPT_V3, SYNTHESIS_PROMPT, RAG_PROMPT_VERSION,
    RAG_SYSTEM_PROMPT, _text_prompt_for, prompt_for, DOMAIN_PROMPT_VERSION, DOMAIN_SYSTEM_PROMPT,
    SUMMARY_PROMPT_VERSION, SUMMARY_SYSTEM_PROMPT, summary_prompt_for, domain_prompt_for)


def create_app(settings: GatewaySettings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    configured = settings or GatewaySettings.from_environment()
    executor = ModelExecutor(configured, transport=transport,
        max_requests=MAX_REQUESTS, max_concurrent=MAX_CONCURRENT_REQUESTS)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await executor.aclose()

    app = FastAPI(title='Personal Knowledge AI Gateway', docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan)

    @app.exception_handler(ModelExecutionError)
    async def execution_error(request, error):
        return JSONResponse(status_code=error.status_code, content={'detail': error.detail})

    async def dispatch(request: Request, route: str):
        authorization = request.headers.get('authorization', '')
        expected = 'Bearer ' + configured.gateway_token
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, detail={'code': 'unauthorized', 'message': 'AI 网关访问凭证无效。'})
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > body_limit(route):
                raise ModelExecutionError(413, detail={'code': 'request_too_large', 'message': '分析资料超出请求大小限制。'})
            body.extend(part)
        return await executor.execute_json(route, body)

    @app.get('/health')
    async def health():
        return {'ready': True, 'node_contract_versions': [1, 2],
            'summary_prompt_version': SUMMARY_PROMPT_VERSION,
            'analysis_image_support': bool(configured.effective_vision_model)}

    @app.post('/v1/analyze', response_model=GatewayResponse)
    async def analyze(request: Request):
        return await dispatch(request, 'analyze')

    @app.post('/v1/answer', response_model=RAGResponse)
    async def answer(request: Request):
        return await dispatch(request, 'answer')

    @app.post('/v1/summarize-node', response_model=DomainSummaryResponse)
    async def summarize_node(request: Request):
        return await dispatch(request, 'summarize-node')

    return app
