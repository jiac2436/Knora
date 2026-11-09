"""Durable domain workflow checkpoints; this coordinator never sends model HTTP."""
from __future__ import annotations

import json
from uuid import UUID, uuid5

from sqlalchemy import select, update

from app import semantic
from app.domain_builds import current_manifest
from app.domain_node_index import node_index_status
from app.domain_summary_jobs import encode
from app.errors import ApiError
from app.models import (AIJob, AIJobStep, Domain, DomainBuild, DomainFullAnalysisJob,
                        DomainSummaryJob, SemanticIndexJob, Source, SourceSearchDocument, SourceSearchTerm,
                        SummaryIndexJob, utcnow)
from app.summary_index import (artifact_index_status, current_summary_artifact,
                               enqueue_summary_index, source_semantic_index_status)

ACTIVE = ('pending', 'running')


def _unsent_children(db, job, status, message):
    progress = json.loads(job.progress_json)
    candidates = [d['job_id'] for d in progress.get('documents', []) if d.get('job_id')]
    ids = [child.id for child in db.scalars(select(AIJob).where(AIJob.id.in_(candidates)))
           if json.loads(child.snapshot_json).get('full_job_id') == job.id]
    if ids:
        # Preserve known pending/completed long-document checkpoints. Cancelling
        # the parent is the scheduling fence; AIJobStep has no cancelled state.
        db.execute(update(AIJob).where(AIJob.id.in_(ids), AIJob.status == 'pending')
            .values(status='cancelled', last_error=message, completed_at=utcnow()))
    build = db.get(DomainBuild, job.build_id) if job.build_id else None
    if build and build.status in ACTIVE and json.loads(build.plan_json).get('full_job_id') == job.id:
        plan = json.loads(build.plan_json)
        node_ids = [str(uuid5(UUID(build.id), step['key'])) for step in plan['steps']]
        db.execute(update(DomainSummaryJob).where(DomainSummaryJob.id.in_(node_ids),
            DomainSummaryJob.status == 'pending').values(status='cancelled', last_error=message,
                                                        completed_at=utcnow()))
        build.status, build.last_error, build.completed_at = status, message, utcnow()


def _stop(db, job, status, message):
    _unsent_children(db, job, status, message)
    job.status, job.last_error, job.completed_at = status, message, utcnow()
    db.commit()
    return True


def interrupt_full_analysis(db):
    """Resume known checkpoints only; an interrupted provider call is never replayed."""
    for job in db.scalars(select(DomainFullAnalysisJob).where(DomainFullAnalysisJob.status.in_(ACTIVE))):
        progress = json.loads(job.progress_json)
        unknown = False
        for item in progress.get('documents', []):
            child = db.get(AIJob, item['job_id']) if item.get('job_id') else None
            if child is None or child.status not in ('running', 'unknown'):
                continue
            if json.loads(child.snapshot_json).get('full_job_id') != job.id:
                unknown = True
                continue
            steps = list(db.scalars(select(AIJobStep).where(AIJobStep.job_id == child.id)))
            if steps and all(step.status in ('pending', 'completed') for step in steps):
                # The last paid checkpoint committed; publication/next unsent step is safe.
                child.status, child.last_error, child.completed_at = 'pending', None, None
            else:
                unknown = True
        build = db.get(DomainBuild, job.build_id) if job.build_id else None
        if build and build.status == 'unknown':
            unknown = True
        if build and db.scalar(select(DomainSummaryJob.id).where(
                DomainSummaryJob.snapshot_json.contains(build.id), DomainSummaryJob.status.in_(('running', 'unknown')))):
            unknown = True
        if unknown:
            _unsent_children(db, job, 'unknown', '服务中断，调用结果未知；不会自动重发')
            job.status, job.last_error, job.completed_at = 'unknown', '服务中断，调用结果未知；不会自动重发', utcnow()
    db.flush()


def _documents(db, job, progress):
    """Queue at most one source; waiters return False so local indexes can run."""
    from app.domain_full_analysis import enqueue_document_checkpoint
    changed = False
    for item in progress['documents']:
        if item['status'] == 'skipped':
            continue
        source = db.get(Source, item['source_id'])
        if item.get('job_id'):
            child = db.get(AIJob, item['job_id'])
            if child is None:
                return _stop(db, job, 'stale', '单篇分析检查点已不存在，未继续')
            status = child.status
            if item['status'] != status:
                item.update(status=status, last_error=child.last_error)
                changed = True
            if status not in (*ACTIVE, 'completed'):
                job.progress_json = encode(progress)
                return _stop(db, job, status if status in ('failed', 'unknown', 'stale', 'cancelled') else 'failed',
                             child.last_error or '单篇分析未完成，后续分析已停止')
            if status in ACTIVE:
                if changed:
                    job.progress_json = encode(progress)
                    db.commit()
                else:
                    db.rollback()
                return changed
        artifact = current_summary_artifact(db, source) if source else None
        force_pending = json.loads(job.snapshot_json).get('reanalyze', False) and not item.get('job_id') and item['status'] == 'waiting'
        if artifact is None or force_pending:
            if item.get('job_id') or item['status'] == 'reused':
                return _stop(db, job, 'stale', '单篇分析产物已失效，后续分析已停止')
            job.progress_json = encode(progress)
            child = enqueue_document_checkpoint(db, job, item['source_id'])
            # The enqueue helper may have refreshed/updated persistent progress.
            progress = json.loads(job.progress_json)
            row = next(d for d in progress['documents'] if d['source_id'] == item['source_id'])
            row.update(status=child.status, job_id=child.id)
            job.progress_json = encode(progress)
            db.commit()
            return True
        if item.get('artifact_id') != artifact.id:
            item['artifact_id'] = artifact.id
            changed = True
        expected = 'completed' if item.get('job_id') else 'reused'
        if item['status'] != expected:
            item['status'] = expected
            changed = True
    # Single-document publication already applies AI labels atomically. Complete
    # summary keyword/vector work before freezing the downstream overview plan.
    for item in progress['documents']:
        if item['status'] == 'skipped':
            continue
        source = db.get(Source, item['source_id'])
        artifact = current_summary_artifact(db, source)
        existing = db.get(SummaryIndexJob, source.id)
        prior = (existing.artifact_id, existing.status) if existing else None
        enqueue_summary_index(db, source)
        db.flush()
        current = db.get(SummaryIndexJob, source.id)
        if prior != ((current.artifact_id, current.status) if current else None):
            changed = True
        state = artifact_index_status(db, artifact)
        if state == 'failed':
            job.progress_json = encode(progress)
            return _stop(db, job, 'partial', '分析结果已保留，但本地摘要索引失败；请检查索引后重新发起')
        if state != 'ready':
            job.progress_json = encode(progress)
            db.commit() if changed else db.rollback()
            return changed
    from app.domain_topics import rebuild_local_topics
    from app.domain_full_analysis import enqueue_overview_checkpoint
    rebuild_local_topics(db, job.owner_id, job.domain_id, 'app')
    job.progress_json = encode(progress)
    build = enqueue_overview_checkpoint(db, job)
    job.build_id, job.phase = build.id, 'overview'
    db.commit()
    return True


def _overview(db, job):
    build = db.get(DomainBuild, job.build_id) if job.build_id else None
    if build is None:
        return _stop(db, job, 'stale', '知识域概览检查点不存在，未继续')
    if build.status in ACTIVE:
        db.rollback()
        return False
    if build.status != 'completed':
        return _stop(db, job, build.status if build.status in ('failed', 'unknown', 'stale', 'cancelled') else 'failed',
                     build.last_error or '概览加工未完成，后续处理已停止')
    job.phase = 'indexing'
    db.commit()
    return True


def _indexing(db, job, progress):
    from app.domain_node_index import ensure_node_index
    from app.semantic import enqueue_semantic_index
    from app.services import reindex_source
    build = db.get(DomainBuild, job.build_id)
    plan = json.loads(build.plan_json) if build else None
    if not build or build.status != 'completed':
        return _stop(db, job, 'stale', '概览加工结果不存在或已停止')
    current_manifest(db, job.owner_id, job.domain_id, 'app', plan['manifest'])
    statuses = dict(semantic_enabled=semantic.settings.semantic_enabled, keyword='ready',
                    original='ready' if semantic.settings.semantic_enabled else 'disabled',
                    summary='ready', topics='ready' if semantic.settings.semantic_enabled else 'disabled',
                    domain='ready' if semantic.settings.semantic_enabled else 'disabled')
    if plan.get('rules') == 'summary-budget-v3':
        statuses['topics'] = 'not_requested'
    pending, failed, changed = False, False, False
    for item in progress['documents']:
        if item['status'] == 'skipped':
            continue
        source = db.get(Source, item['source_id'])
        artifact = current_summary_artifact(db, source) if source else None
        if artifact is None:
            return _stop(db, job, 'stale', '资料或单篇分析结果已变化，未发布完成状态')
        keyword = db.scalar(select(SourceSearchTerm.document_id).join(SourceSearchDocument,
            SourceSearchDocument.id == SourceSearchTerm.document_id).where(
            SourceSearchDocument.owner_id == job.owner_id, SourceSearchDocument.source_id == source.id,
            SourceSearchTerm.term == '__indexed__'))
        raw = source_semantic_index_status(db, source)
        summary = artifact_index_status(db, artifact)
        if 'failed' in (raw, summary):
            # reindex_source also enqueues semantic work. Never let a missing
            # keyword marker accidentally replay a failed local index here.
            statuses.update(original=raw, summary=summary,
                            keyword='ready' if keyword is not None else 'pending')
            if semantic.settings.semantic_enabled:
                statuses['topics'] = statuses['domain'] = 'pending'
            progress['indexing'] = statuses
            job.progress_json = encode(progress)
            return _stop(db, job, 'partial', 'AI 分析与概览已保留，但部分本地索引失败；请检查索引后重新发起')
        keyword_sources = progress.setdefault('keyword_sources', [])
        if keyword is None or source.id not in keyword_sources:
            # The portable keyword cache has no revision column. Rebuild it
            # once against this workflow's frozen current inputs, even if an
            # older __indexed__ marker exists.
            reindex_source(db, source)
            if source.id not in keyword_sources:
                keyword_sources.append(source.id)
            changed = True
        if raw == 'pending':
            raw_job = db.get(SemanticIndexJob, source.id)
            prior = (raw_job.revision_id, raw_job.status) if raw_job else None
            enqueue_semantic_index(db, source)
            db.flush()
            raw_job = db.get(SemanticIndexJob, source.id)
            if raw_job and raw_job.revision_id == source.current_revision_id and raw_job.status == 'completed':
                # Completed metadata without the current vectors is rebuildable local work.
                raw_job.status = 'pending'
            if prior != ((raw_job.revision_id, raw_job.status) if raw_job else None):
                changed = True
        for name, state in (('original', raw), ('summary', summary)):
            if state == 'failed':
                statuses[name], failed = 'failed', True
            elif state not in ('ready', 'disabled'):
                if statuses[name] != 'failed': statuses[name] = 'pending'
                pending = True
    # This commits only local index preparation, never a paid request. Refresh
    # afterwards because it may release the transaction held by this tick.
    ensure_node_index(db, owner_id=job.owner_id)
    db.execute(update(Domain).where(Domain.id == job.domain_id,
        Domain.owner_id == job.owner_id).values(name=Domain.name))
    db.refresh(job)
    if job.status not in ACTIVE:
        return True
    from app.domain_full_analysis import ensure_inputs_current
    ensure_inputs_current(db, job)
    current_manifest(db, job.owner_id, job.domain_id, 'app', plan['manifest'])
    for step in plan['steps']:
        if step['key'] != 'root' and not step.get('published_topic_id'):
            continue
        identifier = step.get('reuse_id') or str(uuid5(UUID(build.id), step['key']))
        status = node_index_status(db, identifier)
        name = 'domain' if step['key'] == 'root' else 'topics'
        if status == 'failed':
            statuses[name], failed = 'failed', True
        elif status not in ('ready', 'disabled'):
            if statuses[name] != 'failed': statuses[name] = 'pending'
            pending = True
    if progress.get('indexing') != statuses:
        progress['indexing'], changed = statuses, True
        job.progress_json = encode(progress)
    if failed:
        return _stop(db, job, 'partial', 'AI 分析与概览已保留，但部分本地索引失败；请检查索引后重新发起')
    if pending:
        if changed:
            job.progress_json = encode(progress)
        db.commit() if changed else db.rollback()
        return changed
    job.status, job.phase, job.last_error, job.completed_at = 'completed', 'completed', None, utcnow()
    job.progress_json = encode(progress)
    db.commit()
    return True


def run_full_analysis_once(db):
    """Advance one workflow while allowing child jobs/indexes to make progress."""
    from app.domain_full_analysis import ensure_inputs_current
    jobs = list(db.scalars(select(DomainFullAnalysisJob).where(DomainFullAnalysisJob.status.in_(ACTIVE))
        .order_by(DomainFullAnalysisJob.created_at, DomainFullAnalysisJob.id)))
    for candidate in jobs:
        identifier = candidate.id
        db.execute(update(Domain).where(Domain.id == candidate.domain_id,
            Domain.owner_id == candidate.owner_id).values(name=Domain.name))
        db.expire_all()
        job = db.get(DomainFullAnalysisJob, identifier)
        if job is None or job.status not in ACTIVE:
            db.rollback()
            continue
        try:
            ensure_inputs_current(db, job)
            if job.status == 'pending':
                job.status, job.started_at = 'running', utcnow()
                db.commit()
                return True
            progress = json.loads(job.progress_json)
            if job.phase == 'documents':
                advanced = _documents(db, job, progress)
            elif job.phase == 'overview':
                advanced = _overview(db, job)
            elif job.phase == 'indexing':
                advanced = _indexing(db, job, progress)
            else:
                advanced = _stop(db, job, 'failed', '全量分析阶段无效，未继续')
            if advanced:
                return True
        except ApiError as error:
            status = 'stale' if 'stale' in error.code or error.code in ('not_found', 'full_inputs_changed', 'full_plan_changed') else 'failed'
            return _stop(db, job, status, error.message)
    return False
