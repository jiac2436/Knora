"""Explicit owner requests, durable checkpoints and version-checked node results."""
import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import gateway_configured
from .ai_contract import StrictModel
from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .domain_contract import DomainSummaryResponse
from .domain_processing import DomainProcessingPolicy, DomainScope
from .domain_runtime import authorize_processing, locked_state, set_processing_paused
from .domain_summary_inputs import build_node_request, validate_node_publication
from .domain_topics import require_owned_domain
from .errors import ApiError
from .models import DomainProcessingPreference, DomainSummaryArtifact, DomainSummaryJob, DomainBuild

router = APIRouter(prefix='/v1/domains', tags=['domain-analysis'])


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def preview_hash(frozen: dict) -> str:
    # A new request ID does not alter the reviewed content.
    value = dict(frozen, request=dict(frozen['request'], request_id='preview'))
    return hashlib.sha256(encode(value).encode()).hexdigest()


def input_reservation(frozen: dict) -> int:
    # Conservative UTF-8 byte reservation plus prompt/envelope allowance.
    # This is a local authorization ceiling, not measured provider billing.
    if frozen['request'].get('contract_version') == 2:
        from .domain_contract import DomainSummaryRequest, summary_input_reservation
        return summary_input_reservation(DomainSummaryRequest.model_validate(frozen['request']))
    return len(encode(frozen['request']).encode()) + 8192


def enqueue_node(db: Session, owner: str, domain: str, scope: DomainScope,
                 request_id: UUID, expected_hash: str, topic_id: str | None = None,
                 confirmed_unknown: bool = False) -> DomainSummaryJob:
    from .model_switch import require_accepting
    if db.get_bind().dialect.name != 'sqlite':
        raise ApiError(409, 'local_sqlite_required', '知识域概览任务目前只支持本地 SQLite')
    state = locked_state(db, owner, domain, scope)
    previous = db.get(DomainSummaryJob, str(request_id))
    if previous:
        frozen = json.loads(previous.snapshot_json)
        if (previous.owner_id != owner or previous.domain_id != domain or previous.scope != scope
                or frozen['request']['node_id'] != (topic_id or domain)
                or preview_hash(frozen) != expected_hash
                or previous.confirmed_unknown != confirmed_unknown):
            raise ApiError(409, 'request_conflict', '相同请求编号不能改变输入或授权范围')
        return previous
    require_accepting(db)
    if scope == 'app':
        from .domain_full_analysis import active_full_job
        if active_full_job(db, owner, domain):
            raise ApiError(409, 'full_running', '知识域全量分析正在执行，请先查看或停止该任务')
    if not gateway_configured(settings):
        raise ApiError(409, 'gateway_not_configured', 'AI 网关尚未配置或地址不安全')
    preference = db.get(DomainProcessingPreference, (domain, scope))
    if preference is None or not DomainProcessingPolicy.model_validate_json(preference.policy_json).enabled:
        raise ApiError(409, 'disabled', '请先确认知识域分析预算')
    active = db.scalar(select(DomainSummaryJob.id).where(DomainSummaryJob.domain_id == domain,
        DomainSummaryJob.scope == scope, DomainSummaryJob.status.in_(('pending', 'running'))))
    active_build = db.scalar(select(DomainBuild.id).where(DomainBuild.domain_id == domain,
        DomainBuild.scope == scope, DomainBuild.status.in_(('pending', 'running'))))
    if active or active_build:
        raise ApiError(409, 'running', '当前范围已有待处理或执行中的概览任务')
    if state.phase == 'unknown' and not confirmed_unknown:
        raise ApiError(409, 'unknown_requires_confirmation', '上次结果未知，确认可能重复产生用量后才能创建新请求')
    frozen = build_node_request(db, owner, domain, scope, request_id, topic_id)
    if preview_hash(frozen) != expected_hash:
        raise ApiError(409, 'stale_preview', '输入已变化，请重新预览后确认')
    estimate = input_reservation(frozen)
    policy = DomainProcessingPolicy.model_validate_json(preference.policy_json)
    if state.used_calls + 1 > policy.max_calls or state.used_input_tokens + estimate > policy.max_input_tokens:
        raise ApiError(409, 'budget_exhausted', '剩余授权预算不足')
    job = DomainSummaryJob(id=str(request_id), owner_id=owner, domain_id=domain, scope=scope,
        generation=state.generation, authorization_id=preference.authorization_id,
        snapshot_json=encode(frozen), gateway_url=settings.ai_gateway_url.rstrip('/'),
        input_tokens=estimate, confirmed_unknown=confirmed_unknown)
    db.add(job); db.flush()
    return job


def job_view(job: DomainSummaryJob) -> dict:
    frozen = json.loads(job.snapshot_json)
    return dict(id=job.id, status=job.status, node_id=frozen['request']['node_id'], scope=job.scope,
        coverage=frozen['coverage'], input_token_reservation=job.input_tokens,
        budget_reserved=job.reserved_at is not None,
        last_error=job.last_error, completed_at=job.completed_at)


def current_node_result(db: Session, owner: str, domain: str, scope: DomainScope,
                        topic_id: str | None = None, *, validation_context=None, artifact=None) -> dict:
    if validation_context:
        validation_context.domain(owner, domain, scope)
        if artifact is None or artifact.id not in validation_context.artifact_ids or (artifact.owner_id, artifact.domain_id, artifact.scope) != (owner, domain, scope):
            raise ApiError(409, 'query_snapshot_scope', '概览产物不属于本次检索快照')
    else:
        require_owned_domain(db, owner, domain)
    from .domain_processing_nodes import logical_node_id
    if validation_context is None:
        artifact = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.owner_id == owner,
            DomainSummaryArtifact.domain_id == domain, DomainSummaryArtifact.scope == scope,
            logical_node_id(DomainSummaryArtifact) == (topic_id or domain))
            .order_by(DomainSummaryArtifact.created_at.desc(), DomainSummaryArtifact.id.desc()))
    if artifact is None:
        return dict(available=False, reason='not_built')
    frozen = json.loads(artifact.snapshot_json)
    published_topic = frozen.get('published_topic_id')
    logical_node = published_topic if published_topic is not None else artifact.node_id
    if validation_context and logical_node != (topic_id or domain):
        raise ApiError(409, 'query_snapshot_scope', '概览节点不属于本次选择')
    response = DomainSummaryResponse.model_validate_json(artifact.response_json)
    try:
        if frozen.get('build_mode'):
            from .domain_builds import validate_build_artifact
            validate_build_artifact(db, owner, domain, scope, frozen, response, validation_context=validation_context)
        else:
            validate_node_publication(db, owner, domain, scope, frozen, response, validation_context=validation_context)
    except ApiError:
        return dict(available=False, reason='stale_inputs')
    points = [response.result.overview, *response.result.insights, *response.result.limitations]
    cited = {c for p in points for c in p.citations}
    cited_sources = {e['source_id'] for e in frozen['request']['evidence'] if e['id'] in cited}
    coverage = dict(frozen['coverage'], cited_source_count=len(cited_sources))
    return dict(available=True, id=artifact.id, result=response.result.model_dump(),
        coverage=coverage, provider=response.provider, model=response.model,
        created_at=artifact.created_at)


class NodeSelection(StrictModel):
    scope: DomainScope = 'app'
    topic_id: str | None = Field(default=None, min_length=1, max_length=36)


class NodeEnqueue(NodeSelection):
    request_id: str

    @field_validator('request_id')
    @classmethod
    def valid_request_id(cls, value):
        return str(UUID(value))
    preview_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed_unknown: bool = False


class ProcessingAuthorization(StrictModel):
    scope: DomainScope = 'app'
    authorization_id: str

    @field_validator('authorization_id')
    @classmethod
    def valid_authorization_id(cls, value):
        return str(UUID(value))
    policy: DomainProcessingPolicy


class ProcessingPause(StrictModel):
    scope: DomainScope = 'app'
    paused: bool


@router.post('/{domain_id}/analysis/preview')
def preview_node(domain_id: str, body: NodeSelection, principal: AppPrincipal = Depends(require_app_principal),
                 db: Session = Depends(get_db)):
    from uuid import uuid4
    frozen = build_node_request(db, principal.owner_id, domain_id, body.scope, uuid4(), body.topic_id)
    return dict(preview_hash=preview_hash(frozen), coverage=frozen['coverage'],
        source_ids=[s['source_id'] for s in frozen['manifest']['sources']], calls=1,
        input_token_reservation=input_reservation(frozen), method='bounded_document_summaries')


@router.put('/{domain_id}/analysis/authorization')
def authorize_node(domain_id: str, body: ProcessingAuthorization,
                   principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    authorize_processing(db, principal.owner_id, domain_id, body.scope, body.policy, UUID(body.authorization_id))
    db.commit()
    return dict(authorized=True)


@router.patch('/{domain_id}/analysis/pause')
def pause_node(domain_id: str, body: ProcessingPause,
               principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    set_processing_paused(db, principal.owner_id, domain_id, body.scope, body.paused)
    db.commit()
    return dict(paused=body.paused)


@router.post('/{domain_id}/analysis/jobs')
def create_node_job(domain_id: str, body: NodeEnqueue, principal: AppPrincipal = Depends(require_app_principal),
                    db: Session = Depends(get_db)):
    job = enqueue_node(db, principal.owner_id, domain_id, body.scope, UUID(body.request_id),
        body.preview_hash, body.topic_id, body.confirmed_unknown)
    db.commit()
    return job_view(job)


@router.get('/{domain_id}/analysis/jobs/{job_id}')
def get_node_job(domain_id: str, job_id: str, principal: AppPrincipal = Depends(require_app_principal),
                 db: Session = Depends(get_db)):
    require_owned_domain(db, principal.owner_id, domain_id)
    job = db.get(DomainSummaryJob, job_id)
    if job is None or job.owner_id != principal.owner_id or job.domain_id != domain_id:
        raise ApiError(404, 'not_found', '概览任务不存在')
    return job_view(job)


@router.post('/{domain_id}/analysis/result')
def get_node_result(domain_id: str, body: NodeSelection, principal: AppPrincipal = Depends(require_app_principal),
                    db: Session = Depends(get_db)):
    result = current_node_result(db, principal.owner_id, domain_id, body.scope, body.topic_id)
    db.commit()
    return result


def purge_node_source(db: Session, source_id: str) -> None:
    """Permanent purge also erases raw excerpts retained in node checkpoints."""
    from sqlalchemy import delete
    from .models import DomainProcessingState
    from .models import DomainFullAnalysisJob, AIJob, utcnow
    for workflow in db.scalars(select(DomainFullAnalysisJob)):
        frozen = json.loads(workflow.snapshot_json)
        if any(item['source_id'] == source_id for item in frozen.get('documents', [])):
            progress = json.loads(workflow.progress_json)
            child_ids = [item['job_id'] for item in progress.get('documents', []) if item.get('job_id')]
            # Stop this workflow's unsent requests before erasing retained metadata.
            # An adopted user-created task remains owned by its original action.
            for child in db.scalars(select(AIJob).where(AIJob.id.in_(child_ids), AIJob.status == 'pending')):
                if json.loads(child.snapshot_json).get('full_job_id') == workflow.id:
                    child.status, child.last_error, child.completed_at = 'cancelled', '资料永久删除，全量分析已停止，未发送', utcnow()
            if workflow.build_id:
                build = db.get(DomainBuild, workflow.build_id)
                if build:
                    build.status, build.last_error, build.completed_at = 'cancelled', '资料永久删除，全量分析已停止', utcnow()
            db.delete(workflow)
    db.flush()
    for model in (DomainSummaryJob, DomainSummaryArtifact):
        for row in db.scalars(select(model)):
            frozen = json.loads(row.snapshot_json)
            if any(item['source_id'] == source_id for name in ('manifest', 'global_manifest', 'topic_manifest', 'processing_manifest')
                   for item in frozen.get(name, {}).get('sources', [])):
                if model is DomainSummaryJob and row.status == 'running':
                    state = db.get(DomainProcessingState, (row.domain_id, row.scope))
                    if state and state.request_id == row.id:
                        state.phase = 'unknown'
                db.execute(delete(model).where(model.id == row.id))
    db.flush()

    for build in db.scalars(select(DomainBuild)):
        plan = json.loads(build.plan_json)
        if any(item['source_id'] == source_id for item in plan['manifest']['sources']):
            db.delete(build)
    db.flush()
