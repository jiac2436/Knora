"""Explicit, version-bound local AI tasks and user-confirmed organization."""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .ai_contract import AnalysisRequest, AnalysisResult, validate_result
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import AIArtifact, AIJob, AIJobStep, Domain, EvidenceChunk, PersonalContext, Project, Source, SourceProject, SourceRevision, KnowledgeTag, KnowledgeTagAlias, TagRejection, TagSuggestion, utcnow

router = APIRouter(prefix="/v1/ai")


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def gateway_configured(configuration=None) -> bool:
    configuration = configuration or settings
    if not configuration.ai_gateway_url or not configuration.ai_gateway_token:
        return False
    try:
        url = urlsplit(configuration.ai_gateway_url)
        _ = url.port
    except ValueError:
        return False
    return (bool(url.hostname) and not url.username and not url.password and not url.query and not url.fragment
            and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})))


def visible_source(db: Session, owner_id: str, source_id: str) -> Source:
    source = db.scalar(select(Source).where(Source.id == source_id, Source.owner_id == owner_id,
        Source.deleted_at.is_(None), Source.processing_status.not_in(("deleting", "deleted"))))
    if source is None:
        raise ApiError(404, "not_found", "资料不存在")
    return source


def context_state(db: Session, source: Source) -> dict:
    context = db.get(PersonalContext, source.id)
    domain = db.get(Domain, source.domain_id)
    projects = db.scalars(select(Project).join(SourceProject, SourceProject.project_id == Project.id)
        .where(SourceProject.source_id == source.id).order_by(Project.id)).all()
    return context_view(source, context, domain, projects)


def context_view(source: Source, context, domain, projects) -> dict:
    """Shared serialization for live reads and request-local preloaded rows."""
    return {"title": source.title, "domain_id": source.domain_id,
            "domain_name": domain.name if domain else None,
            "projects": [{"id": p.id, "name": p.name, "goal": p.goal,
                          "constraints": p.constraints, "background": p.background} for p in projects],
            "personal": {"version": context.version, "reason": context.reason, "notes": context.notes,
                         "applicability": context.applicability} if context else None}


def is_stale(db: Session, source: Source, snapshot: dict, current_context: dict | None = None) -> bool:
    from .ai_images import images_current
    return source.current_revision_id != snapshot["revision_id"] or (
        current_context if current_context is not None else context_state(db, source)) != snapshot["context"] or not images_current(db, source, snapshot)


def freeze_snapshot(db: Session, source: Source, kind: str, request_id: str, contract_version: int = 2) -> dict:
    revision = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    if revision is None or source.processing_status != "usable":
        raise ApiError(409, "ai_source_not_ready", "请等待资料处理完成后再分析")
    from .ai_long_analysis import evidence_plan
    evidence, sections, analysis_plan = evidence_plan(db, source)
    if contract_version == 3 and not any(c.isalnum() for item in evidence for c in item["text"]):
        raise ApiError(409, "ai_evidence_unreadable", "正文缺少可分析的文字，请先检查资料解析结果")
    if not evidence:
        raise ApiError(409, "ai_evidence_missing", "资料没有可分析的正文证据")
    domains = db.scalars(select(Domain).where(Domain.owner_id == source.owner_id).order_by(Domain.id).limit(201)).all()
    projects = db.scalars(select(Project).where(Project.owner_id == source.owner_id).order_by(Project.id).limit(201)).all()
    if len(domains) > 200 or len(projects) > 200:
        raise ApiError(409, "ai_taxonomy_too_large", "分类或项目数量超过本次分析支持的范围")
    tags = db.scalars(select(KnowledgeTag).where(KnowledgeTag.owner_id == source.owner_id)
                      .order_by(KnowledgeTag.id).limit(501)).all()
    rejections = db.scalars(select(TagRejection).where(TagRejection.source_id == source.id,
        TagRejection.revision_id == revision.id).order_by(TagRejection.dimension, TagRejection.normalized_name).limit(501)).all()
    if len(tags) > 500 or len(rejections) > 500:
        raise ApiError(409, "ai_taxonomy_too_large", "标签目录或拒绝记录超过本次分析支持的范围")
    aliases = {}
    for alias in db.scalars(select(KnowledgeTagAlias).where(KnowledgeTagAlias.owner_id == source.owner_id).order_by(KnowledgeTagAlias.tag_id, KnowledgeTagAlias.normalized_name)):
        aliases.setdefault(alias.tag_id, []).append(alias.name)
    if any(len(values) > 100 for values in aliases.values()):
        raise ApiError(409, "ai_taxonomy_too_large", "单个标签别名数量超过本次分析支持的范围")
    snapshot = {"analysis_sections": sections, "analysis_plan": analysis_plan, "phase": "single", "contract_version": contract_version, "tags": [{"id": tag.id, "name": tag.name, "dimension": tag.dimension,
            "aliases": aliases.get(tag.id, [])} for tag in tags],
            "rejected_tags": [{"name": item.normalized_name, "dimension": item.dimension} for item in rejections],
            "request_id": request_id, "kind": kind, "source_id": source.id, "title": source.title,
            "revision_id": revision.id, "context": context_state(db, source), "evidence": evidence,
            "domains": [{"id": d.id, "name": d.name} for d in domains],
            "projects": [{"id": p.id, "name": p.name} for p in projects],
            "warnings": ["资料超过分析范围，本次仅分析已列出的部分正文证据，不能视为完整原件总结。"] if analysis_plan["coverage"] == "partial" else []}
    if contract_version == 3:
        from .ai_images import bind_images
        bind_images(db, source, snapshot, settings.upload_dir)
    from .model_configuration import frozen_model_binding
    snapshot.update(frozen_model_binding(settings))
    return snapshot


def job_view(db: Session, job: AIJob) -> dict:
    artifact_id = db.scalar(select(AIArtifact.id).where(AIArtifact.job_id == job.id))
    snapshot = json.loads(job.snapshot_json)
    steps = db.scalars(select(AIJobStep).where(AIJobStep.job_id == job.id).order_by(AIJobStep.ordinal)).all()
    can_resume = bool(steps) and (job.status == "pending" and all(step.status in {"pending", "completed"} for step in steps)
        or job.status == "unknown" and all(step.status in {"pending", "completed"} for step in steps))
    from .ai_pipeline import pipeline_view
    total_steps = len(steps) if steps else 1
    completed_steps = sum(step.status == "completed" for step in steps) if steps else int(artifact_id is not None)
    return {"contract_version": snapshot.get("contract_version", 1), **pipeline_view(db, job=job), "can_resume": can_resume, "completed_steps": completed_steps, "total_steps": total_steps,
            "analysis_plan": snapshot.get("analysis_plan"), "id": job.id, "source_id": job.source_id, "kind": job.kind, "status": job.status,
            "last_error": job.last_error, "created_at": job.created_at,
            "completed_at": job.completed_at, "artifact_id": artifact_id}


def artifact_view(db: Session, artifact: AIArtifact) -> dict:
    source = visible_source(db, artifact.owner_id, artifact.source_id)
    snapshot = json.loads(artifact.snapshot_json)
    from .ai_tag_suggestions import suggestion_view
    stale = is_stale(db, source, snapshot)
    candidates = db.scalars(select(TagSuggestion).where(TagSuggestion.artifact_id == artifact.id,
        TagSuggestion.owner_id == artifact.owner_id).order_by(TagSuggestion.created_at, TagSuggestion.id)).all()
    from .ai_pipeline import pipeline_view
    from .knowledge_tags import source_tag_views
    return {"auto_tags_current": source_tag_views(db, source)["tag_details"] if snapshot.get("contract_version") == 3 else [],
            "contract_version": snapshot.get("contract_version", 1), **pipeline_view(db, artifact=artifact), "coverage": snapshot.get("analysis_plan", {}).get("coverage", "partial" if snapshot.get("warnings") else "full"),
            "phase": snapshot.get("phase", "single"), "section_index": snapshot.get("section_index"),
            "analysis_plan": snapshot.get("analysis_plan"), "tag_suggestions": [suggestion_view(db, item, stale) for item in candidates], "id": artifact.id, "source_id": artifact.source_id, "job_id": artifact.job_id,
            "revision_id": artifact.revision_id, "result": json.loads(artifact.result_json),
            "evidence": snapshot["evidence"], "provider": artifact.provider, "model": artifact.model,
            "prompt_version": artifact.prompt_version, "usage": json.loads(artifact.usage_json),
            "stale": is_stale(db, source, snapshot), "applied_at": artifact.applied_at, "created_at": artifact.created_at}


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    kind: Literal["analyze", "organize"]
    contract_version: Literal[2, 3] = 2
    request_key: UUID
    plan_hash: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    kind: Literal["analyze", "organize"]
    contract_version: Literal[2, 3] = 2


@router.post("/plan")
def preview_plan(body: PlanRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .ai_long_analysis import plan_hash, preflight_snapshot
    source = visible_source(db, principal.owner_id, str(body.source_id))
    db.execute(update(Source).where(Source.id == source.id).values(updated_at=Source.updated_at))
    db.expire_all(); source = visible_source(db, principal.owner_id, source.id)
    if body.contract_version == 3 and body.kind != "analyze":
        raise ApiError(422, "ai_kind_invalid", "新版 AI 分析仅支持统一分析")
    snapshot = freeze_snapshot(db, source, body.kind, "00000000-0000-0000-0000-000000000000", body.contract_version)
    snapshot["gateway_url"] = settings.ai_gateway_url.rstrip("/")
    preflight_snapshot(snapshot)
    view = {**snapshot["analysis_plan"], "plan_hash": plan_hash(snapshot)}
    db.rollback()
    return view


@router.get("/config")
def config(principal: AppPrincipal = Depends(require_app_principal)) -> dict:
    return {"configured": gateway_configured()}


@router.post("/jobs", status_code=202)
def create_job(body: JobRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .model_switch import require_accepting
    if body.contract_version == 3 and body.kind != "analyze":
        raise ApiError(422, "ai_kind_invalid", "新版 AI 分析仅支持统一分析")
    intent_body = {"source_id": str(body.source_id), "kind": body.kind}
    if body.contract_version != 2:
        intent_body["contract_version"] = body.contract_version
    if body.plan_hash is not None:
        intent_body["plan_hash"] = body.plan_hash
    intent = canonical(intent_body)
    digest = hashlib.sha256(intent.encode()).hexdigest()
    key = str(body.request_key)
    # Serialize identities across all versions and sources: legacy requests
    # must also conflict with identifiers reused by a v3 task. Match the
    # owner-before-source order of manual vocabulary/assignment edits.
    from .knowledge_tags import lock_owner
    lock_owner(db, principal.owner_id)
    existing = db.scalar(select(AIJob).where(AIJob.owner_id == principal.owner_id, AIJob.request_key == key))
    if existing is None:
        for candidate in db.scalars(select(AIJob).where(AIJob.owner_id == principal.owner_id,
                AIJob.snapshot_json.contains(key))):
            alias = next((item for item in json.loads(candidate.snapshot_json).get("request_aliases", [])
                if item["key"] == key), None)
            if alias:
                if alias["digest"] != digest:
                    raise ApiError(409, "ai_request_conflict", "请求标识已用于其他分析任务")
                visible_source(db, principal.owner_id, candidate.source_id)
                view = job_view(db, candidate)
                db.rollback()
                return view
    if existing:
        visible_source(db, principal.owner_id, existing.source_id)
        if existing.request_sha256 != digest:
            raise ApiError(409, "ai_request_conflict", "请求标识已用于其他分析任务")
        view = job_view(db, existing)
        db.rollback()
        return view
    require_accepting(db)
    if not gateway_configured():
        raise ApiError(503, "ai_not_configured", "请先配置 AI 网关")
    source = visible_source(db, principal.owner_id, str(body.source_id))
    # Acquire the same source write lock as edits before freezing all evidence/context.
    from .domain_full_analysis import active_full_job
    if active_full_job(db, principal.owner_id, source.domain_id):
        raise ApiError(409, 'full_running', '当前知识域正在全量分析，请查看该任务进度')
    db.execute(update(Source).where(Source.id == source.id).values(updated_at=Source.updated_at))
    db.expire_all()
    source = visible_source(db, principal.owner_id, source.id)
    from .models import new_id
    job_id = new_id()
    snapshot = freeze_snapshot(db, source, body.kind, job_id, body.contract_version)
    # Bind the user's selected destination without persisting its access token.
    snapshot["gateway_url"] = settings.ai_gateway_url.rstrip("/")
    from .ai_long_analysis import plan_hash, preflight_snapshot, initialize_steps
    preflight_snapshot(snapshot)
    expected_plan_hash = plan_hash(snapshot)
    if snapshot["analysis_sections"] and body.plan_hash is None:
        raise ApiError(409, "ai_plan_confirmation_required", "长文分析需要先预览并确认多次模型调用")
    if body.plan_hash is not None and body.plan_hash != expected_plan_hash:
        raise ApiError(409, "ai_plan_changed", "资料、标签目录或 AI 配置已变更，请重新预览分析计划")
    if body.contract_version == 3:
        for active in db.scalars(select(AIJob).where(AIJob.owner_id == principal.owner_id,
                AIJob.source_id == source.id, AIJob.kind == body.kind,
                AIJob.status.in_(("pending", "running"))).order_by(AIJob.created_at)):
            active_snapshot = json.loads(active.snapshot_json)
            active_snapshot.pop("analysis_request", None)
            active_snapshot["phase"] = "single"
            if plan_hash(active_snapshot) == expected_plan_hash:
                saved_snapshot = json.loads(active.snapshot_json)
                aliases = saved_snapshot.setdefault("request_aliases", [])
                if len(aliases) >= 256:
                    raise ApiError(409, "ai_active_task_exists", "已有分析任务，请查看当前任务进度")
                aliases.append({"key": key, "digest": digest})
                active.snapshot_json = canonical(saved_snapshot)
                db.commit()
                return job_view(db, active)
    job = AIJob(id=job_id, owner_id=principal.owner_id, source_id=source.id, kind=body.kind,
                request_key=key, request_sha256=digest, snapshot_json=canonical(snapshot), status="pending")
    db.add(job)
    if snapshot["analysis_sections"]:
        db.flush()
        initialize_steps(db, job, snapshot)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(AIJob).where(AIJob.owner_id == principal.owner_id, AIJob.request_key == key))
        if existing is None or existing.request_sha256 != digest:
            raise ApiError(409, "ai_request_conflict", "请求标识已用于其他分析任务")
        job = existing
    return job_view(db, job)


@router.get("/jobs")
def list_jobs(source_id: str | None = None, limit: int = Query(50, ge=1, le=100),
              principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    statement = select(AIJob).join(Source, Source.id == AIJob.source_id).where(AIJob.owner_id == principal.owner_id,
        Source.owner_id == principal.owner_id, Source.deleted_at.is_(None), Source.processing_status.not_in(("deleting", "deleted")))
    if source_id:
        visible_source(db, principal.owner_id, source_id)
        statement = statement.where(AIJob.source_id == source_id)
    return {"items": [job_view(db, j) for j in db.scalars(statement.order_by(AIJob.created_at.desc()).limit(limit))]}


@router.get("/artifacts")
def list_artifacts(source_id: str | None = None, limit: int = Query(50, ge=1, le=100),
                   principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    statement = select(AIArtifact).join(Source, Source.id == AIArtifact.source_id).where(AIArtifact.owner_id == principal.owner_id,
        Source.owner_id == principal.owner_id, Source.deleted_at.is_(None), Source.processing_status.not_in(("deleting", "deleted")))
    if source_id:
        visible_source(db, principal.owner_id, source_id)
        statement = statement.where(AIArtifact.source_id == source_id)
    return {"items": [artifact_view(db, a) for a in db.scalars(statement.order_by(AIArtifact.created_at.desc()).limit(limit))]}


def owned_artifact(db: Session, owner_id: str, artifact_id: str) -> AIArtifact:
    artifact = db.scalar(select(AIArtifact).where(AIArtifact.id == artifact_id, AIArtifact.owner_id == owner_id))
    if artifact is None:
        raise ApiError(404, "not_found", "分析结果不存在")
    visible_source(db, owner_id, artifact.source_id)
    return artifact


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    return artifact_view(db, owned_artifact(db, principal.owner_id, artifact_id))


@router.post("/artifacts/{artifact_id}/apply")
def apply_artifact(artifact_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    artifact = owned_artifact(db, principal.owner_id, artifact_id)
    db.execute(update(Source).where(Source.id == artifact.source_id).values(updated_at=Source.updated_at))
    db.expire_all()
    artifact = owned_artifact(db, principal.owner_id, artifact_id)
    if artifact.applied_at:
        return artifact_view(db, artifact)
    source = visible_source(db, principal.owner_id, artifact.source_id)
    try:
        snapshot = json.loads(artifact.snapshot_json)
        validation_snapshot = snapshot.get("analysis_request", snapshot)
        request = AnalysisRequest.model_validate({key: value for key, value in validation_snapshot.items()
                                                 if key in AnalysisRequest.model_fields})
        result = validate_result(request, AnalysisResult.model_validate_json(artifact.result_json))
        if request.source_id != artifact.source_id or request.revision_id != artifact.revision_id:
            raise ValueError("Artifact snapshot binding mismatch")
    except (ValueError, TypeError, AttributeError):
        raise ApiError(409, "ai_artifact_invalid", "分析结果或证据快照无效，请重新分析")
    if is_stale(db, source, snapshot):
        raise ApiError(409, "ai_artifact_stale", "资料或个人上下文已更新，请重新分析")
    if result.organizing_suggestion is None:
        raise ApiError(409, "ai_no_suggestion", "没有可应用的整理建议")
    suggestion = result.organizing_suggestion.model_dump()
    domain_id = suggestion["domain_id"] or source.domain_id
    if not db.scalar(select(Domain.id).where(Domain.id == domain_id, Domain.owner_id == principal.owner_id)):
        raise ApiError(409, "ai_candidate_changed", "建议的分类已不可用")
    project_ids = list(dict.fromkeys(suggestion["project_ids"]))
    valid = set(db.scalars(select(Project.id).where(Project.id.in_(project_ids), Project.owner_id == principal.owner_id)))
    if valid != set(project_ids):
        raise ApiError(409, "ai_candidate_changed", "建议的项目已不可用")
    source.domain_id = domain_id
    db.execute(delete(SourceProject).where(SourceProject.source_id == source.id))
    for project_id in project_ids:
        db.add(SourceProject(source_id=source.id, project_id=project_id))
    context = db.get(PersonalContext, source.id)
    if context:
        context.version += 1
    artifact.applied_at = utcnow()
    db.commit()
    return artifact_view(db, artifact)


def owned_job(db: Session, owner: str, job_id: str) -> AIJob:
    job = db.scalar(select(AIJob).where(AIJob.id == job_id, AIJob.owner_id == owner))
    if job is None:
        raise ApiError(404, "not_found", "分析任务不存在")
    visible_source(db, owner, job.source_id)
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    job = owned_job(db, principal.owner_id, job_id)
    return {**job_view(db, job), "steps": [{"id": step.id, "ordinal": step.ordinal, "phase": step.phase,
        "status": step.status, "error": step.error} for step in db.scalars(select(AIJobStep)
            .where(AIJobStep.job_id == job.id).order_by(AIJobStep.ordinal))]}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    job = owned_job(db, principal.owner_id, job_id)
    db.execute(update(AIJob).where(AIJob.id == job.id, AIJob.status.in_(("pending", "running")))
        .values(status="cancelled", last_error="用户已停止后续调用；已发送请求可能产生用量", completed_at=utcnow()))
    db.commit(); db.expire_all()
    return get_job(job_id, principal, db)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .model_switch import require_accepting
    require_accepting(db)
    job = owned_job(db, principal.owner_id, job_id)
    db.execute(update(Source).where(Source.id == job.source_id).values(updated_at=Source.updated_at))
    db.expire_all(); job = owned_job(db, principal.owner_id, job_id)
    source = visible_source(db, principal.owner_id, job.source_id)
    steps = db.scalars(select(AIJobStep).where(AIJobStep.job_id == job.id)).all()
    safe_pending = job.status == "pending" and all(step.status in {"pending", "completed"} for step in steps)
    safe_interrupted = job.status == "unknown" and all(step.status in {"pending", "completed"} for step in steps)
    if not steps or not (safe_pending or safe_interrupted):
        raise ApiError(409, "ai_resume_not_safe", "已有请求失败、结果未知或任务已停止，不能自动重复发送；请检查后创建新任务")
    if is_stale(db, source, json.loads(job.snapshot_json)):
        raise ApiError(409, "ai_artifact_stale", "资料或个人上下文已更新，请重新预览分析")
    job.status, job.last_error, job.completed_at = "pending", None, None
    db.commit()
    return get_job(job_id, principal, db)
