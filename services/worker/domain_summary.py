"""Single-attempt node gateway transport, reusable by checkpointed jobs."""
import json

import httpx
from app.embedded_gateway import gateway_transport

from app.ai import gateway_configured
from app.config import settings
from app.model_configuration import model_binding_matches, knowledge_snapshot
from app.domain_contract import DomainSummaryRequest, DomainSummaryResponse, validate_domain_summary


def validation_failure_text(detail):
    """Persist fixed categories only; never upstream messages or source text."""
    reasons = {
        'node_reading_budget': '概览文字超出阅读上限',
        'node_repeated_statement': '概览存在空白或重复陈述',
        'node_evidence_reference': '原文引用缺失、重复或不存在',
        'node_citation_budget': '引用原文超出合成上限',
        'output_incomplete': '模型输出被截断',
        'invalid_json': '模型返回的JSON无效',
        'invalid_output': '结构或字段校验失败',
    }
    reason = detail.get('reason')
    if not isinstance(reason, str):
        reason = None
    explanation = reasons.get(reason, '结构或证据校验失败')
    fields = detail.get('fields')
    if isinstance(fields, list):
        safe = sorted({f for f in fields if isinstance(f, str)
                       and f in {'overview', 'insights', 'limitations', 'contract_version'}})
        if safe:
            explanation += '（字段：' + '、'.join(safe) + '）'
    return '概览生成失败：' + explanation + '；本次可能已产生用量，不会自动重试'


def call_node_gateway(request: DomainSummaryRequest, transport: httpx.BaseTransport | None = None):
    if not gateway_configured(settings):
        return "failed", "AI 网关尚未配置或地址不安全", None
    try:
        with httpx.Client(transport=gateway_transport(transport), timeout=httpx.Timeout(120, connect=10),
                          follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", settings.ai_gateway_url.rstrip("/") + "/v1/summarize-node",
                headers={"Authorization": "Bearer " + settings.ai_gateway_token},
                json=request.model_dump()) as reply:
                content = bytearray()
                for part in reply.iter_bytes():
                    if len(content) + len(part) > 256 * 1024:
                        return "unknown", "AI 返回内容超限，可能已经产生用量；不会自动重试", None
                    content.extend(part)
                if reply.status_code != 200:
                    if 400 <= reply.status_code < 500:
                        return "failed", "AI 网关拒绝请求，请检查配置后手动处理", None
                    try:
                        detail = json.loads(content).get("detail", {})
                    except (ValueError, AttributeError):
                        detail = {}
                    if isinstance(detail, dict) and detail.get("code") == "invalid_provider_result":
                        return "failed", validation_failure_text(detail), None
                    return "unknown", "AI 请求结果未知，可能已产生用量；不会自动重试", None
        response = DomainSummaryResponse.model_validate_json(content)
        if response.request_id != request.request_id:
            raise ValueError("Node response binding mismatch")
        validate_domain_summary(request, response.result)
        return "completed", None, response
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unknown", "AI 请求中断，可能已产生用量；不会自动重试", None
    except (ValueError, TypeError):
        return "failed", "概览响应未通过结构、请求编号或原文引用校验；不会自动重试", None


def interrupt_node_jobs(db):
    """After a hard stop, a committed claim is never replayed automatically."""
    from sqlalchemy import update
    from app.models import DomainSummaryJob, utcnow
    db.execute(update(DomainSummaryJob).where(DomainSummaryJob.status == 'running').values(
        status='unknown', last_error='服务中断，概览调用结果未知；不会自动重发', completed_at=utcnow()))


def run_node_once(db, transport=None):
    """One explicit queued request, reserve/commit before HTTP, publish atomically."""
    from uuid import UUID
    from sqlalchemy import select
    from app.domain_runtime import locked_state, reserve_domain_call
    from app.domain_summary_inputs import build_node_request, validate_node_publication
    from app.errors import ApiError
    from app.models import DomainProcessingPreference, DomainSummaryJob, DomainSummaryArtifact, utcnow
    from app.models import DomainBuild
    from app.domain_schedule import automatic_paused, is_automatic, queued_job_build

    candidates = db.scalars(select(DomainSummaryJob).where(DomainSummaryJob.status == 'pending')
                            .order_by(DomainSummaryJob.created_at, DomainSummaryJob.id)).all()
    job = next((candidate for candidate in candidates
                if not automatic_paused(db, queued_job_build(db, candidate))), None)
    if job is None:
        return False
    identifier = job.id
    try:
        state = locked_state(db, job.owner_id, job.domain_id, job.scope)
        db.refresh(job)
        if job.status != 'pending':
            db.rollback()
            return False
        frozen = json.loads(job.snapshot_json)
        build = db.get(DomainBuild, frozen['build_id']) if frozen.get('build_id') else None
        if automatic_paused(db, build):
            db.rollback()
            return False
        request = DomainSummaryRequest.model_validate(frozen['request'])
        preference = db.get(DomainProcessingPreference, (job.domain_id, job.scope))
        full_build = bool(build and json.loads(build.plan_json).get('full_job_id'))
        if not full_build and (preference is None or preference.authorization_id != job.authorization_id):
            reason = 'authorization_changed'
        elif (job.gateway_url != settings.ai_gateway_url.rstrip('/') or not gateway_configured(settings)
                or not model_binding_matches(frozen, settings)):
            reason = 'gateway_changed_or_unavailable'
        else:
            if frozen.get('build_mode'):
                from app.domain_builds import validate_build_job
                validate_build_job(db, job, frozen)
                current = frozen
            else:
                current = build_node_request(db, job.owner_id, job.domain_id, job.scope,
                    UUID(identifier), request.node_id if request.kind == 'topic' else None)
            if current != frozen:
                reason = 'stale_inputs'
            else:
                if full_build:
                    from app.domain_full_runtime import reserve_full_node_call
                    reason = reserve_full_node_call(db, build, job, state)
                else:
                    reason = reserve_domain_call(db, job.owner_id, job.domain_id, job.scope,
                        generation=job.generation, request_id=UUID(identifier), input_tokens=job.input_tokens,
                        now=utcnow(), manual=not is_automatic(build), confirmed_unknown=job.confirmed_unknown,
                        coalesced=is_automatic(build))
        if reason != 'reserved':
            job.status = 'stale' if reason in ('stale_inputs', 'stale_generation') else 'cancelled'
            job.last_error = '任务未发送：' + reason
            job.completed_at = utcnow()
            db.commit()
            return True
        job.status, job.reserved_at = 'running', utcnow()
        db.commit()
    except ApiError:
        db.rollback()
        job = db.get(DomainSummaryJob, identifier)
        if job:
            job.status, job.last_error, job.completed_at = 'stale', '任务输入已变化，未发送', utcnow()
            db.commit()
        return True

    # Release the SQLite write lock and refresh the committed claim before sending.
    status, error, response = call_node_gateway(request, transport)
    job = db.get(DomainSummaryJob, identifier)
    if job is None:
        db.rollback()
        return True
    state = locked_state(db, job.owner_id, job.domain_id, job.scope)
    db.refresh(job)
    if job.status != 'running' or state.request_id != identifier:
        db.rollback()
        return True
    if status == 'completed':
        try:
            if frozen.get('build_mode'):
                from app.domain_builds import validate_build_job
                validate_build_job(db, job, frozen)
            else:
                validate_node_publication(db, job.owner_id, job.domain_id, job.scope, frozen, response)
        except ApiError:
            status, error = 'stale', '资料、主题或授权范围已变化，结果未发布；本次可能已产生用量'
        else:
            db.add(DomainSummaryArtifact(id=identifier, owner_id=job.owner_id, domain_id=job.domain_id,
                scope=job.scope, node_id=request.node_id, snapshot_json=json.dumps(knowledge_snapshot(frozen), ensure_ascii=False),
                response_json=response.model_dump_json()))
    job.status, job.last_error, job.completed_at = status, error, utcnow()
    if status == 'completed' and frozen.get('build_mode'):
        state.phase = 'dirty'
    elif status == 'completed' and request.kind == 'domain':
        state.phase = 'partial'  # Bounded cited excerpts never imply whole-domain comprehension.
        state.dirty_since = state.last_changed_at = None
    elif status == 'completed' or status == 'stale':
        state.phase = 'dirty'
    else:
        state.phase = status
    db.commit()
    return True
