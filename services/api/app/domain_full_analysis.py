"""One reviewed authorization for a bounded, checkpointed whole-domain analysis.

Read APIs never create work. The coordinator sends one document at a time and
uses the existing document/build workers; this module never calls a model.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timezone
from math import ceil
from typing import Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import ai
from .ai_contract import StrictModel
from .ai_long_analysis import MAX_WIRE_BYTES, initialize_steps, plan_hash, preflight_snapshot
from .ai_images import MAX_BATCH_TILES
from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .domain_builds import MAX_STEPS, PARENT_INPUT_RESERVATION, plan_budget, prepare_plan
from .domain_runtime import locked_state
from .domain_query_snapshot import input_batches
from .domain_summary_inputs import document_input
from .domain_topics import require_owned_domain, source_scope
from .errors import ApiError
from .knowledge_tags import lock_owner
from .models import (AIArtifact, AIJob, DomainBuild, DomainFullAnalysisJob, DomainSummaryJob,
                     DomainProcessingState, DomainTopic, DomainTopicMember, EvidenceChunk, ExperienceRecord,
                     KnowledgeTag, KnowledgeTagAlias, PersonalContext, Project, Source, SourceProject,
                     SourceRevision, SourceImage, TagAssignment, TagRejection, utcnow)
from .summary_index import current_summary_artifact
from .summary_retrieval import summary_evidence

router = APIRouter(prefix='/v1/domains', tags=['domain-full-analysis'])
RULES = 'domain-full-v2-images'
ACTIVE = ('pending', 'running')
DOCUMENT_CALL_RESERVATION = MAX_WIRE_BYTES + MAX_BATCH_TILES * 1024 + 8192
MAX_SOURCES = 5000


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def active_full_job(db, owner, domain):
    return db.scalar(select(DomainFullAnalysisJob).where(DomainFullAnalysisJob.owner_id == owner,
        DomainFullAnalysisJob.domain_id == domain, DomainFullAnalysisJob.status.in_(ACTIVE))
        .order_by(DomainFullAnalysisJob.created_at, DomainFullAnalysisJob.id).limit(1))


class FullSelection(StrictModel):
    scope: Literal['app'] = 'app'
    reanalyze: bool = False


class FullEnqueue(FullSelection):
    request_id: str
    preview_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: Literal[True]
    confirmed_unknown: bool = False

    @field_validator('request_id', mode='before')
    @classmethod
    def normalized_request_id(cls, value):
        if not isinstance(value, (str, UUID)):
            raise ValueError('Request ID must be a UUID')
        return str(UUID(str(value)))

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value


def current_full_snapshot(db, owner, domain):
    """Inputs that users can change, excluding this pipeline's own AI outputs.

    Generated tags/summaries/automatic topic candidates are intentionally not
    frozen: publishing them is the expected progress, rather than a user edit.
    """
    selected = require_owned_domain(db, owner, domain)
    sources = list(db.scalars(source_scope(owner, domain, 'app').order_by(Source.id).limit(MAX_SOURCES + 1)))
    if len(sources) > MAX_SOURCES:
        raise ApiError(409, 'full_scope_too_large', '全量分析最多支持 5000 篇资料，请缩小知识域范围')
    # Every dispatch rechecks the full reviewed scope. Batch dependencies so
    # this remains a bounded number of SQL reads, rather than N queries/source.
    ids = [s.id for s in sources]
    revisions, contexts = {}, {}
    evidence_rows, manual_rows, rejected_rows, experience_rows, projects = (defaultdict(list) for _ in range(5))
    image_rows = defaultdict(list)
    for batch in input_batches(ids):
        for image in db.scalars(select(SourceImage).where(SourceImage.source_id.in_(batch))
                .order_by(SourceImage.source_id, SourceImage.ordinal, SourceImage.id)):
            image_rows[image.source_id].append(image)
        current_revisions = select(Source.current_revision_id).where(Source.id.in_(batch))
        for revision in db.scalars(select(SourceRevision).where(SourceRevision.source_id.in_(batch),
                SourceRevision.id.in_(current_revisions))):
            revisions[revision.id] = revision
        for row in db.execute(select(EvidenceChunk.revision_id, EvidenceChunk.id, EvidenceChunk.ordinal,
                EvidenceChunk.text, EvidenceChunk.locator_kind, EvidenceChunk.locator_start, EvidenceChunk.locator_end)
                .where(EvidenceChunk.revision_id.in_(current_revisions))
                .order_by(EvidenceChunk.revision_id, EvidenceChunk.ordinal, EvidenceChunk.id)):
            evidence_rows[row.revision_id].append(list(row[1:]))
        contexts.update((context.source_id, context) for context in db.scalars(select(PersonalContext)
            .where(PersonalContext.source_id.in_(batch))))
        for source_id, project in db.execute(select(SourceProject.source_id, Project).join(Project,
                Project.id == SourceProject.project_id).where(SourceProject.source_id.in_(batch))
                .order_by(Project.id)):
            projects[source_id].append(project)
        for row in db.execute(select(TagAssignment.source_id, KnowledgeTag.id, KnowledgeTag.name, KnowledgeTag.dimension)
                .join(KnowledgeTag, TagAssignment.tag_id == KnowledgeTag.id)
                .where(TagAssignment.source_id.in_(batch), TagAssignment.origin == 'manual',
                    KnowledgeTag.owner_id == owner).order_by(TagAssignment.source_id, KnowledgeTag.id)):
            manual_rows[row.source_id].append(list(row[1:]))
        for row in db.execute(select(TagRejection.source_id, TagRejection.revision_id,
                TagRejection.dimension, TagRejection.normalized_name).where(TagRejection.source_id.in_(batch))
                .order_by(TagRejection.source_id, TagRejection.revision_id, TagRejection.dimension, TagRejection.normalized_name)):
            rejected_rows[row.source_id].append(list(row[1:]))
        for row in db.execute(select(ExperienceRecord.source_id, ExperienceRecord.id, ExperienceRecord.project_id,
                ExperienceRecord.result, ExperienceRecord.notes, ExperienceRecord.verified_at)
                .where(ExperienceRecord.source_id.in_(batch), ExperienceRecord.owner_id == owner)
                .order_by(ExperienceRecord.id)):
            experience_rows[row.source_id].append([*row[1:5], row.verified_at.isoformat()])
    aliases = defaultdict(list)
    manual_ids = sorted({tag[0] for rows in manual_rows.values() for tag in rows})
    for batch in input_batches(manual_ids):
        for row in db.execute(select(KnowledgeTagAlias.tag_id, KnowledgeTagAlias.name).where(
                KnowledgeTagAlias.owner_id == owner, KnowledgeTagAlias.tag_id.in_(batch))
                .order_by(KnowledgeTagAlias.tag_id, KnowledgeTagAlias.name)):
            aliases[row.tag_id].append([row.tag_id, row.name])
    dependencies = []
    for source in sources:
        revision = revisions.get(source.current_revision_id)
        manual_aliases = [alias for tag in manual_rows[source.id] for alias in aliases[tag[0]]]
        payload = dict(title=source.title, kind=source.kind, coverage=source.coverage,
            processing_status=source.processing_status, visibility=source.visibility,
            raw_input=source.raw_input, raw_url=source.raw_url, canonical_url=source.canonical_url,
            revision_id=source.current_revision_id, text=revision.extracted_text if revision else None,
            context=ai.context_view(source, contexts.get(source.id), selected, projects[source.id]),
            evidence=evidence_rows[source.current_revision_id], manual_tags=manual_rows[source.id],
            manual_tag_aliases=manual_aliases, rejected_tags=rejected_rows[source.id],
            experiences=experience_rows[source.id])
        if image_rows[source.id]:
            from .services import current_web_base_revision
            base = current_web_base_revision(db, source)
            image_revision = base.id if base else source.current_revision_id
            payload['images'] = [[image.id, image.revision_id, image.ordinal, image.status, image.sha256,
                image.file_key, image.size_bytes, image.mime_type, image.width, image.height]
                for image in image_rows[source.id] if image.revision_id == image_revision]
        dependencies.append(dict(source_id=source.id, revision_id=source.current_revision_id,
                                 input_sha256=digest(payload)))
    topics = list(db.scalars(select(DomainTopic).where(DomainTopic.owner_id == owner,
        DomainTopic.domain_id == domain, DomainTopic.scope == 'app').order_by(DomainTopic.id)))
    members = list(db.execute(select(DomainTopicMember.topic_id, DomainTopicMember.source_id,
        DomainTopicMember.included).where(DomainTopicMember.topic_id.in_([t.id for t in topics]),
        DomainTopicMember.manual.is_(True)).order_by(DomainTopicMember.topic_id, DomainTopicMember.source_id)))
    edited = {row.topic_id for row in members}
    structure = dict(topics=[[t.id, t.name, t.parent_id, t.name_fixed, t.automatic_key]
        for t in topics if t.automatic_key is None or t.name_fixed or t.parent_id or t.id in edited],
        members=[list(row) for row in members])
    from .model_configuration import frozen_model_binding
    return dict(**frozen_model_binding(settings), owner_id=owner, domain_id=domain, domain_name=selected.name,
        gateway_url=settings.ai_gateway_url.rstrip('/'), sources=dependencies, structure_sha256=digest(structure))


def ensure_inputs_current(db, job):
    frozen = json.loads(job.snapshot_json)
    if frozen.get('rules') != RULES:
        raise ApiError(409, 'full_image_consent_changed', '图文分析范围已升级，请重新预览并确认；已完成结果保留')
    if current_full_snapshot(db, job.owner_id, job.domain_id) != frozen['inputs']:
        raise ApiError(409, 'full_inputs_changed', '知识域资料、备注或人工分类已变化，本次全量分析已停止，请重新预览')


def _valid_document(db, source):
    artifact = current_summary_artifact(db, source)
    if artifact and not json.loads(artifact.snapshot_json).get('images'):
        # A text-only result is readable history, but cannot satisfy a newly
        # reviewed whole-domain visual analysis grant.
        any_image = db.scalar(select(SourceImage.id).where(SourceImage.source_id == source.id).limit(1))
        if any_image:
            from .ai_images import image_rows
            if image_rows(db, source):
                return None
    return artifact if artifact and summary_evidence(db, source, artifact) is not None else None


def _active_document(db, source, snapshot):
    for job in db.scalars(select(AIJob).where(AIJob.owner_id == source.owner_id,
            AIJob.source_id == source.id, AIJob.kind == 'analyze', AIJob.status.in_(('pending', 'running')))
            .order_by(AIJob.created_at, AIJob.id)):
        saved = json.loads(job.snapshot_json)
        if saved.get('contract_version') == 3 and not ai.is_stale(db, source, saved):
            return job
        raise ApiError(409, 'full_document_busy', '资料已有不同输入的分析任务，请先等待其结束')
    return None


def _overview_step_bound(leaves):
    from .domain_overview_plan import FAN_IN
    steps = leaves
    while leaves > FAN_IN:
        groups, remainder = divmod(leaves, FAN_IN)
        steps += groups + int(remainder > 1)
        leaves = groups + int(remainder > 0)
    return steps + int(leaves > 1)


def _minimum_overview_steps(db, owner, domain, source_ids):
    # Themes no longer require individual paid branches. Even the shortest
    # summaries have a request item ceiling, independent of overlapping tags.
    return _overview_step_bound(ceil(len(source_ids) / 128))


def _previously_analyzed(db, owner, ids):
    # Portable backups preserve final artifacts without operational AIJob rows.
    analyzed = set()
    for batch in input_batches(ids):
        analyzed.update(db.scalars(select(AIArtifact.source_id).where(AIArtifact.owner_id == owner,
            AIArtifact.source_id.in_(batch),
            func.json_extract(AIArtifact.snapshot_json, '$.kind') == 'analyze',
            func.coalesce(func.json_extract(AIArtifact.snapshot_json, '$.phase'), '') != 'segment',
            func.coalesce(func.json_extract(AIArtifact.snapshot_json, '$.wire_request.phase'), '') != 'segment',
            func.json_extract(AIArtifact.result_json, '$.summary').is_not(None)).distinct()))
    return analyzed


def _overview_covers_selected(plan, documents):
    skipped = {d['source_id'] for d in documents if d['status'] == 'skipped'}
    return (plan['coverage']['processed_source_count'] == len(documents) - len(skipped)
            and set(plan['coverage']['excluded']) == skipped)


def preview_full(db, owner, domain, reanalyze=False):
    inputs = current_full_snapshot(db, owner, domain)
    documents, blocked, unknown = [], [], []
    stale_ids = set()
    previous = _previously_analyzed(db, owner, [d['source_id'] for d in inputs['sources']])
    for dependency in inputs['sources']:
        source = db.get(Source, dependency['source_id'])
        valid = _valid_document(db, source)
        if valid is None and source.id in previous:
            stale_ids.add(source.id)
        artifact = valid if not reanalyze else None
        skipped = not reanalyze and artifact is None and source.id in previous
        item = dict(source_id=source.id, title=source.title,
                    status='skipped' if skipped else 'reused' if artifact else 'waiting', artifact_id=artifact.id if artifact else None,
                    calls=0, input_token_reservation=0)
        try:
            if skipped:
                documents.append(item)
                continue
            if artifact:
                from .domain_processing import DomainSourceDependency
                _, evidence, reason = document_input(db, DomainSourceDependency(
                    **dependency, summary_artifact_id=artifact.id))
                if reason:
                    raise ApiError(409, 'full_summary_unbounded', '已有摘要或引用超出知识域加工范围，请先检查该资料的分析结果')
            else:
                snapshot = ai.freeze_snapshot(db, source, 'analyze', '00000000-0000-0000-0000-000000000000', 3)
                snapshot['gateway_url'] = inputs['gateway_url']
                preflight_snapshot(snapshot)
                if snapshot['analysis_plan']['coverage'] != 'full':
                    raise ApiError(409, 'full_document_too_large', '正文超过单篇分段分析上限，请拆分资料后再执行全量分析')
                _active_document(db, source, snapshot)
                item['calls'] = snapshot['analysis_plan']['calls']
                item['input_token_reservation'] = item['calls'] * DOCUMENT_CALL_RESERVATION
                item['analysis_plan'] = snapshot['analysis_plan']
                prior_unknown = db.scalars(select(AIJob).where(AIJob.owner_id == owner,
                    AIJob.source_id == source.id, AIJob.kind == 'analyze', AIJob.status == 'unknown'))
                if any(not ai.is_stale(db, source, json.loads(prior.snapshot_json)) for prior in prior_unknown):
                    unknown.append(dict(source_id=source.id, title=source.title))
        except ApiError as error:
            blocked.append(dict(source_id=source.id, title=source.title, reason=error.message, code=error.code))
            item['status'] = 'blocked'
        documents.append(item)
    if inputs['sources'] and _minimum_overview_steps(db, owner, domain,
            {item['source_id'] for item in documents if item['status'] != 'skipped'}) > MAX_STEPS:
        blocked.append(dict(source_id=None, title=inputs['domain_name'], code='full_tree_too_large',
            reason='资料数量超过当前概览加工范围，请拆分知识域后再分析'))
    analyze = sum(item['status'] == 'waiting' for item in documents)
    # At most one leaf per valid document: a single overview too large for one
    # request is explicitly blocked, never split into repeated original calls.
    from .domain_contract import SUMMARY_INPUT_BUDGET
    skipped_ids = {d['source_id'] for d in documents if d['status'] == 'skipped'}
    eligible = len(documents) - len(skipped_ids)
    overview_calls = min(MAX_STEPS, _overview_step_bound(eligible)) if eligible else 0
    overview = dict(calls=overview_calls, input_token_reservation=overview_calls * SUMMARY_INPUT_BUDGET,
                    estimate='bounded_upper_limit')
    if not analyze and not blocked and eligible:
        actual = prepare_plan(db, owner, domain, 'app', excluded_source_ids=skipped_ids)
        if not _overview_covers_selected(actual, documents):
            blocked.append(dict(source_id=None, title=inputs['domain_name'], code='full_incomplete_plan',
                reason='当前摘要不能覆盖全部资料，请检查单篇分析结果'))
        overview = dict(**plan_budget(actual), estimate='current_exact_plan')
    if documents and not eligible:
        blocked.append(dict(source_id=None, title=inputs['domain_name'], code='full_no_eligible',
            reason='所有文章的分析均已过期；勾选“重新分析”后再更新知识库'))
    if not eligible:
        overview = dict(calls=0, input_token_reservation=0, estimate='empty')
    budget = dict(calls=sum(item['calls'] for item in documents) + overview['calls'],
        input_token_reservation=sum(item['input_token_reservation'] for item in documents) + overview['input_token_reservation'],
        document_calls=sum(item['calls'] for item in documents), overview_calls=overview['calls'],
        overview_input_token_reservation=overview['input_token_reservation'], estimate=overview['estimate'])
    state = db.get(DomainProcessingState, (domain, 'app'))
    prior_unknown_full = db.scalar(select(DomainFullAnalysisJob).where(
        DomainFullAnalysisJob.owner_id == owner, DomainFullAnalysisJob.domain_id == domain,
        DomainFullAnalysisJob.status == 'unknown').order_by(DomainFullAnalysisJob.created_at.desc()).limit(1))
    unknown_full_current = bool(prior_unknown_full and json.loads(prior_unknown_full.snapshot_json)['inputs'] == inputs)
    requires_unknown = bool(unknown) or bool(state and state.phase == 'unknown') or unknown_full_current
    frozen = dict(rules=RULES, reanalyze=reanalyze, stale_sources=len(stale_ids), inputs=inputs, documents=documents, budget=budget,
                  requires_unknown_confirmation=requires_unknown, unknown_sources=unknown)
    return dict(preview_hash=digest(frozen), total_sources=len(documents),
        reanalyze=reanalyze, stale_sources=len(stale_ids),
        skipped_sources=len(skipped_ids),
        reused_sources=sum(item['status'] == 'reused' for item in documents), analyze_sources=analyze,
        blocked_sources=blocked, calls=budget['calls'], input_token_reservation=budget['input_token_reservation'],
        budget_basis=overview['estimate'], configured=ai.gateway_configured(settings),
        requires_unknown_confirmation=requires_unknown, unknown_sources=unknown,
        documents=[dict(source_id=d['source_id'], title=d['title'],
            status='analyze' if d['status'] == 'waiting' else d['status']) for d in documents], _snapshot=frozen)


def full_job_view(db, job):
    progress, budget = map(json.loads, (job.progress_json, job.budget_json))
    documents = progress.get('documents', [])
    for item in documents:
        child = db.get(AIJob, item.get('job_id')) if item.get('job_id') else None
        if child and item['status'] not in ('reused', 'completed'):
            item.update(status=child.status, last_error=child.last_error)
    completed = sum(d['status'] in ('completed', 'reused', 'skipped') for d in documents)
    current = next((d['title'] for d in documents if d['status'] in ('pending', 'running')), None)
    def timestamp(value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
    from .domain_builds import build_view
    build = db.get(DomainBuild, job.build_id) if job.build_id else None
    overview_progress = build_view(db, build) if build else None
    frozen = json.loads(job.snapshot_json)
    from .domain_full_usage import reported_usage
    return dict(id=job.id, request_id=job.id, reanalyze=frozen.get('reanalyze', False),
        stale_sources=frozen.get('stale_sources', 0), skipped_sources=sum(d['status'] == 'skipped' for d in documents),
        token_usage=reported_usage(db, job, documents), status=job.status, phase=job.phase,
        total_sources=len(documents), reused_sources=sum(d['status'] == 'reused' for d in documents),
        analyzed_sources=sum(d['status'] == 'completed' for d in documents), completed_sources=completed,
        current_source_title=current, build_id=job.build_id, last_error=job.last_error,
        created_at=timestamp(job.created_at), started_at=timestamp(job.started_at), completed_at=timestamp(job.completed_at),
        budget=budget, documents=documents, indexing=progress.get('indexing'),
        overview_coverage=json.loads(build.plan_json).get('coverage') if build else progress.get('overview_coverage'),
        overview_progress=overview_progress, scope='app')


def enqueue_full(db, owner, domain, body):
    from .model_switch import require_accepting
    lock_owner(db, owner)
    require_owned_domain(db, owner, domain)
    identity = str(body.request_id)
    intent = dict(domain_id=domain, scope='app', preview_hash=body.preview_hash,
        confirmed=body.confirmed, confirmed_unknown=body.confirmed_unknown)
    if body.reanalyze:
        intent['reanalyze'] = True
    request_digest = digest(intent)
    prior = db.get(DomainFullAnalysisJob, identity)
    if prior:
        if (prior.owner_id, prior.domain_id, prior.request_sha256) != (owner, domain, request_digest):
            raise ApiError(409, 'full_request_conflict', '相同请求编号不能改变资料范围或授权')
        return prior
    require_accepting(db)
    if db.get_bind().dialect.name != 'sqlite':
        raise ApiError(409, 'local_sqlite_required', '全量分析仅支持本地 SQLite')
    if not ai.gateway_configured(settings):
        raise ApiError(503, 'ai_not_configured', '请先配置 AI 网关')
    active = active_full_job(db, owner, domain)
    nodes = db.scalar(select(DomainSummaryJob.id).where(DomainSummaryJob.domain_id == domain,
        DomainSummaryJob.scope == 'app', DomainSummaryJob.status.in_(('pending', 'running'))).limit(1))
    builds = db.scalar(select(DomainBuild.id).where(DomainBuild.domain_id == domain,
        DomainBuild.scope == 'app', DomainBuild.status.in_(('pending', 'running'))).limit(1))
    if active or nodes or builds:
        raise ApiError(409, 'full_running', '知识域已有分析任务，请先查看或停止当前任务')
    preview = preview_full(db, owner, domain, body.reanalyze)
    if preview['preview_hash'] != body.preview_hash:
        raise ApiError(409, 'full_stale_preview', '资料或可复用分析已变化，请重新预览')
    if not preview['total_sources']:
        raise ApiError(409, 'full_empty', '知识域暂无可分析资料')
    if preview['blocked_sources']:
        raise ApiError(409, 'full_blocked', '存在无法分析的资料，请先处理预览中的阻塞原因')
    state = locked_state(db, owner, domain, 'app')
    if (preview['requires_unknown_confirmation'] or state.phase == 'unknown') and not body.confirmed_unknown:
        raise ApiError(409, 'unknown_requires_confirmation', '上次调用结果未知，请明确确认可能重复产生用量')
    frozen = preview['_snapshot']
    frozen.update(preview_hash=body.preview_hash, confirmed_unknown=body.confirmed_unknown)
    budget = dict(frozen['budget'], used_calls=0, used_input_tokens=0, actual_input_tokens=None,
                  reserved_document_jobs=[])
    progress = dict(documents=[{key: item[key] for key in ('source_id', 'title', 'status', 'artifact_id')}
                               for item in frozen['documents']])
    job = DomainFullAnalysisJob(id=identity, owner_id=owner, domain_id=domain,
        request_sha256=request_digest, snapshot_json=encode(frozen), progress_json=encode(progress),
        budget_json=encode(budget), status='pending', phase='documents')
    db.add(job); db.flush()
    return job


def reserve_document(db, job, child, planned):
    budget = json.loads(job.budget_json)
    reserved = budget.setdefault('reserved_document_jobs', [])
    if child.id in reserved:
        return
    calls, tokens = planned['calls'], planned['input_token_reservation']
    if budget['used_calls'] + calls > budget['calls'] or budget['used_input_tokens'] + tokens > budget['input_token_reservation']:
        raise ApiError(409, 'full_budget_exceeded', '单篇分析超过本次授权上限，未发送')
    budget['used_calls'] += calls
    budget['used_input_tokens'] += tokens
    reserved.append(child.id)
    job.budget_json = encode(budget)


def enqueue_document_checkpoint(db, job, source_id):
    ensure_inputs_current(db, job)
    if job.status not in ('pending', 'running') or job.phase != 'documents':
        raise ApiError(409, 'full_stopped', '全量分析任务已停止，未继续派发')
    frozen = json.loads(job.snapshot_json)
    planned = next((d for d in frozen['documents'] if d['source_id'] == source_id and d['status'] == 'waiting'), None)
    if planned is None:
        raise ApiError(409, 'full_document_not_authorized', '资料不在本次待分析范围内')
    source = ai.visible_source(db, job.owner_id, source_id)
    identifier = str(uuid5(UUID(job.id), 'document:' + source_id))
    child = db.get(AIJob, identifier)
    if child:
        if (child.owner_id, child.source_id) != (job.owner_id, source_id):
            raise ApiError(409, 'full_request_conflict', '分析检查点范围不符')
        reserve_document(db, job, child, planned)
        return child
    snapshot = ai.freeze_snapshot(db, source, 'analyze', identifier, 3)
    snapshot['gateway_url'] = frozen['inputs']['gateway_url']
    preflight_snapshot(snapshot)
    if snapshot['analysis_plan']['coverage'] != 'full' or snapshot['analysis_plan']['calls'] > planned['calls']:
        raise ApiError(409, 'full_plan_changed', '实际单篇加工计划超出预览范围，未发送')
    child = _active_document(db, source, snapshot)
    if child is None:
        key = str(uuid5(UUID(job.id), 'document-request:' + source_id))
        intent = dict(source_id=source_id, kind='analyze', contract_version=3, plan_hash=plan_hash(snapshot))
        snapshot['full_job_id'] = job.id
        child = AIJob(id=identifier, owner_id=job.owner_id, source_id=source_id, kind='analyze',
            request_key=key, request_sha256=digest(intent), snapshot_json=encode(snapshot), status='pending')
        # A coordinator catches API errors and may commit the stopped parent.
        # Reserve before adding the child, so exhausted consent cannot leave a
        # new pending document outside the parent's recorded progress.
        reserve_document(db, job, child, planned)
        db.add(child); db.flush()
        if snapshot['analysis_sections']:
            initialize_steps(db, child, snapshot)
    else:
        reserve_document(db, job, child, planned)
    return child


def enqueue_overview_checkpoint(db, job):
    ensure_inputs_current(db, job)
    if job.status not in ('pending', 'running'):
        raise ApiError(409, 'full_stopped', '全量分析任务已停止')
    identifier = str(uuid5(UUID(job.id), 'overview'))
    existing = db.get(DomainBuild, identifier)
    if existing:
        if ((existing.owner_id, existing.domain_id, existing.scope) != (job.owner_id, job.domain_id, 'app')
                or json.loads(existing.plan_json).get('full_job_id') != job.id):
            raise ApiError(409, 'full_request_conflict', '概览检查点范围不符')
        return existing
    state = locked_state(db, job.owner_id, job.domain_id, 'app')
    confirmed = json.loads(job.snapshot_json)
    skipped = {d['source_id'] for d in confirmed['documents'] if d['status'] == 'skipped'}
    plan = prepare_plan(db, job.owner_id, job.domain_id, 'app', excluded_source_ids=skipped)
    budget, confirmed = json.loads(job.budget_json), json.loads(job.snapshot_json)
    actual = plan_budget(plan)
    if not _overview_covers_selected(plan, confirmed['documents']):
        raise ApiError(409, 'full_incomplete_overview', '部分单篇结果无法纳入知识域概览，未进行不完整加工')
    if (budget['used_calls'] + actual['calls'] > budget['calls'] or
            budget['used_input_tokens'] + actual['input_token_reservation'] > budget['input_token_reservation']):
        raise ApiError(409, 'full_budget_exceeded', '后续概览计划超过本次预览授权上限，未发送')
    plan['full_job_id'] = job.id
    build = DomainBuild(id=identifier, owner_id=job.owner_id, domain_id=job.domain_id, scope='app',
        generation=state.generation, authorization_id=job.id, gateway_url=confirmed['inputs']['gateway_url'],
        plan_json=encode(plan), confirmed_unknown=confirmed['confirmed_unknown'], status='pending')
    db.add(build); db.flush()
    return build


def _owned_job(db, owner, domain, identifier):
    require_owned_domain(db, owner, domain)
    job = db.get(DomainFullAnalysisJob, identifier)
    if job is None or (job.owner_id, job.domain_id) != (owner, domain):
        raise ApiError(404, 'not_found', '全量分析任务不存在')
    return job


@router.post('/{domain_id}/analysis/full/preview')
def preview_endpoint(domain_id: str, body: FullSelection, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    preview = preview_full(db, principal.owner_id, domain_id, body.reanalyze)
    return {key: value for key, value in preview.items() if key != '_snapshot'}


@router.post('/{domain_id}/analysis/full/jobs', status_code=202)
def enqueue_endpoint(domain_id: str, body: FullEnqueue, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    job = enqueue_full(db, principal.owner_id, domain_id, body)
    db.commit()
    return full_job_view(db, job)


@router.get('/{domain_id}/analysis/full/latest')
def latest_endpoint(domain_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    require_owned_domain(db, principal.owner_id, domain_id)
    job = db.scalar(select(DomainFullAnalysisJob).where(DomainFullAnalysisJob.owner_id == principal.owner_id,
        DomainFullAnalysisJob.domain_id == domain_id).order_by(DomainFullAnalysisJob.created_at.desc(), DomainFullAnalysisJob.id.desc()).limit(1))
    return dict(job=full_job_view(db, job) if job else None)


@router.get('/{domain_id}/analysis/full/jobs/{job_id}')
def get_endpoint(domain_id: str, job_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return full_job_view(db, _owned_job(db, principal.owner_id, domain_id, job_id))


@router.post('/{domain_id}/analysis/full/jobs/{job_id}/cancel')
def cancel_endpoint(domain_id: str, job_id: str, body: FullSelection, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    lock_owner(db, principal.owner_id)
    job = _owned_job(db, principal.owner_id, domain_id, job_id)
    if job.status not in ACTIVE:
        db.rollback()
        return full_job_view(db, job)
    progress = json.loads(job.progress_json)
    child_ids = [d['job_id'] for d in progress['documents'] if d.get('job_id')]
    running = db.scalar(select(AIJob.id).where(AIJob.id.in_(child_ids), AIJob.status == 'running').limit(1))
    owned_child_ids = [child.id for child in db.scalars(select(AIJob).where(AIJob.id.in_(child_ids)))
                       if json.loads(child.snapshot_json).get('full_job_id') == job.id]
    # Mark even an in-flight document as cancelled so a long-document worker
    # cannot advance to its next paid section after this response returns.
    db.execute(update(AIJob).where(AIJob.id.in_(owned_child_ids), AIJob.status.in_(('pending', 'running')))
        .values(status='cancelled', last_error='用户停止全量分析，已发送请求可能仍产生用量', completed_at=utcnow()))
    if job.build_id:
        build = db.get(DomainBuild, job.build_id)
        if (build and (build.owner_id, build.domain_id, build.scope) == (job.owner_id, job.domain_id, 'app')
                and json.loads(build.plan_json).get('full_job_id') == job.id):
            node_scope = (DomainSummaryJob.owner_id == job.owner_id, DomainSummaryJob.domain_id == job.domain_id,
                          DomainSummaryJob.scope == 'app', DomainSummaryJob.snapshot_json.contains(build.id))
            node_running = db.scalar(select(DomainSummaryJob.id).where(
                *node_scope, DomainSummaryJob.status == 'running').limit(1))
            running = running or node_running
            db.execute(update(DomainSummaryJob).where(*node_scope,
                DomainSummaryJob.status.in_(('pending', 'running'))).values(status='cancelled',
                last_error='用户停止全量分析，已发送请求可能仍产生用量', completed_at=utcnow()))
            build.status, build.last_error, build.completed_at = 'cancelled', '用户停止全量分析', utcnow()
    for item in progress['documents']:
        if item['status'] in ('waiting', 'pending'):
            item['status'] = 'cancelled'
    job.status = 'unknown' if running else 'cancelled'
    if running:
        # Also protect the pre-existing overview entry point, which consults
        # this state before allowing a possibly duplicated paid update.
        state = db.get(DomainProcessingState, (job.domain_id, 'app'))
        if state:
            state.phase = 'unknown'
    job.last_error = '已停止后续加工；已发送请求结果未知，可能已产生用量' if running else '用户停止全量分析'
    job.completed_at = utcnow()
    job.progress_json = encode(progress)
    db.commit()
    return full_job_view(db, job)
