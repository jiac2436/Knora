"""One explicit local AI task, with no automatic replay of paid requests."""
from __future__ import annotations

import json

import httpx
from app.embedded_gateway import gateway_transport
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai import canonical, gateway_configured
from app.model_configuration import knowledge_snapshot
from app.ai_contract import AnalysisRequest, GatewayResponse, validate_result
from app.config import settings
from app.models import AIArtifact, AIJob, Source, KnowledgeTag, TagSuggestion, utcnow


MAX_RESPONSE_BYTES = 512 * 1024
WIRE_FIELDS = frozenset(AnalysisRequest.model_fields)


def _finish(db: Session, job_id: str, status: str, message: str | None,
            response: GatewayResponse | None = None) -> None:
    """Serialize publication with edits/deletion, without holding a lock over HTTP."""
    db.rollback()
    job = db.get(AIJob, job_id)
    if job is None or job.status != "running":
        return
    source_id, owner_id = job.source_id, job.owner_id
    if response is not None and json.loads(job.snapshot_json).get("contract_version") == 3:
        # Match the owner-before-source order used by manual tag edits and v3
        # task creation. No database lock is held over the provider request.
        from app.knowledge_tags import lock_owner
        lock_owner(db, owner_id)
    locked = db.execute(update(Source).where(Source.id == source_id,
        Source.owner_id == owner_id, Source.deleted_at.is_(None),
        Source.processing_status.not_in(("deleting", "deleted")))
        .values(updated_at=Source.updated_at)).rowcount
    db.expire_all()
    job = db.get(AIJob, job_id)
    if job is None or job.status != "running":
        db.rollback()
        return
    if not locked:
        status, message, response = "cancelled", "资料已删除，分析结果未发布", None
    if response is not None:
        snapshot = json.loads(job.snapshot_json)
        if snapshot.get("contract_version") == 3:
            from app.ai import is_stale
            from app.ai_auto_tags import catalog_unchanged
            source = db.get(Source, job.source_id)
            if (is_stale(db, source, snapshot)
                    or not catalog_unchanged(db, snapshot, job.owner_id, response.result.tag_suggestions)):
                job.status, job.last_error, job.completed_at = "cancelled", "资料或标签目录已变更，分析结果未发布；请重新分析", utcnow()
                db.commit()
                return
        result = response.result.model_dump()
        result["warnings"] = (snapshot.get("warnings", []) + result["warnings"])[:20]
        artifact = AIArtifact(owner_id=job.owner_id, source_id=job.source_id, job_id=job.id,
            revision_id=snapshot["revision_id"], snapshot_json=canonical(knowledge_snapshot(snapshot)),
            result_json=canonical(result), provider=response.provider, model=response.model,
            prompt_version=response.prompt_version, usage_json=canonical(response.usage.model_dump()))
        db.add(artifact); db.flush()
        if snapshot.get("contract_version") == 3:
            from app.ai_auto_tags import publish_tags
            from app.summary_index import enqueue_summary_index
            publish_tags(db, source, artifact, response.result.tag_suggestions)
            enqueue_summary_index(db, source)
        if snapshot.get("contract_version", 1) == 2:
            from app.ai_tag_suggestions import rejection_matches
            from app.knowledge_tags import normalize
            source = db.get(Source, job.source_id)
            for candidate in response.result.tag_suggestions:
                name, key = normalize(candidate.name)
                if candidate.tag_id:
                    current = db.get(KnowledgeTag, candidate.tag_id)
                    frozen = next(t for t in snapshot["tags"] if t["id"] == candidate.tag_id)
                    if current is None or current.owner_id != job.owner_id or current.name != frozen["name"] or current.dimension != frozen["dimension"]:
                        warning = "标签目录已变更，相关标签候选未发布；请重新分析。"
                        if warning not in result["warnings"]:
                            result["warnings"] = result["warnings"][:19] + [warning]
                        artifact.result_json = canonical(result)
                        continue
                rejected = rejection_matches(db, source, candidate.dimension, name, candidate.tag_id)
                db.add(TagSuggestion(owner_id=job.owner_id, source_id=job.source_id, artifact_id=artifact.id,
                    revision_id=artifact.revision_id, tag_id=candidate.tag_id, name=name, normalized_name=key,
                    dimension=candidate.dimension, reason=candidate.reason, evidence_json=canonical(candidate.citations),
                    status="rejected" if rejected else "pending", decided_at=utcnow() if rejected else None))
    job.status, job.last_error, job.completed_at = status, message, utcnow()
    db.commit()


def run_ai_once(db: Session, transport: httpx.BaseTransport | None = None) -> bool:
    """Claim one task; callers may inject a transport for synthetic verification."""
    job_id = db.scalar(select(AIJob.id).where(AIJob.status == "pending")
        .order_by(AIJob.created_at, AIJob.id).limit(1))
    if job_id is None:
        return False
    claimed = db.execute(update(AIJob).where(AIJob.id == job_id, AIJob.status == "pending")
        .values(status="running", last_error=None)).rowcount
    db.commit()
    if not claimed:
        return True
    job = db.get(AIJob, job_id)
    if job is None:
        return True
    source = db.get(Source, job.source_id)
    if (source is None or source.owner_id != job.owner_id or source.deleted_at is not None
            or source.processing_status in {"deleting", "deleted"}):
        _finish(db, job_id, "cancelled", "资料已删除，分析任务已取消")
        return True
    snapshot = json.loads(job.snapshot_json)
    if snapshot.get("full_job_id"):
        from app.domain_full_analysis import ensure_inputs_current
        from app.errors import ApiError
        from app.models import DomainFullAnalysisJob
        full = db.get(DomainFullAnalysisJob, snapshot["full_job_id"])
        allowed = (full is not None and full.status in ("pending", "running") and full.phase == "documents"
            and (full.owner_id, full.domain_id) == (job.owner_id, source.domain_id))
        try:
            if not allowed:
                raise ApiError(409, "full_stopped", "全量分析已停止，资料未发送")
            ensure_inputs_current(db, full)
        except ApiError:
            _finish(db, job_id, "cancelled", "全量分析范围已变化或任务已停止，资料未发送")
            return True
    if snapshot.get("contract_version") == 3:
        from app.ai import is_stale
        if is_stale(db, source, snapshot):
            _finish(db, job_id, "cancelled", "资料或个人上下文已更新，资料尚未发送；请重新分析")
            return True
    if snapshot.get("analysis_sections"):
        from app.ai_long_analysis import run_long_step
        return run_long_step(db, job_id, transport)
    try:
        request = AnalysisRequest.model_validate({key: value for key, value in snapshot.items() if key in WIRE_FIELDS})
        if request.request_id != job.id or request.source_id != job.source_id or request.kind != job.kind:
            raise ValueError("Snapshot binding mismatch")
    except (ValueError, TypeError):
        _finish(db, job_id, "failed", "分析任务快照无效，请重新创建任务")
        return True
    if not gateway_configured(settings):
        _finish(db, job_id, "failed", "AI 网关尚未配置或地址不安全")
        return True
    from app.model_configuration import model_binding_matches
    if snapshot.get("gateway_url") != settings.ai_gateway_url.rstrip("/"):
        _finish(db, job_id, "failed", "AI 网关地址已变更，资料尚未发送；请重新创建分析任务")
        return True
    if not model_binding_matches(snapshot, settings):
        _finish(db, job_id, "failed", "模型配置代次已变更或旧任务缺少绑定，资料尚未发送；请重新确认分析")
        return True
    db.rollback()
    status, message, response = call_gateway(request, transport, image_files=snapshot.get('image_files', {}))
    _finish(db, job_id, status, message, response)
    return True


def call_gateway(request: AnalysisRequest, transport: httpx.BaseTransport | None = None, *, image_files=None) -> tuple[str, str | None, GatewayResponse | None]:
    """Exactly one HTTP attempt; no provider response or credentials in errors."""
    from app.ai_images import hydrate_images
    try:
        wire = hydrate_images(request, image_files or {}, settings.upload_dir)
    except (OSError, ValueError):
        return 'failed', '图片原件缺失、已变化或超过图文传输限制；尚未调用模型，请重新检查图片', None
    try:
        with httpx.Client(transport=gateway_transport(transport), timeout=httpx.Timeout(120, connect=10),
                          follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", settings.ai_gateway_url.rstrip("/") + "/v1/analyze",
                headers={"Authorization": "Bearer " + settings.ai_gateway_token},
                json=wire.model_dump()) as reply:
                if reply.status_code != 200:
                    if reply.status_code == 400 and request.images:
                        return 'failed', '网关尚未配置支持图片的模型；请配置 PKA_AI_VISION_MODEL 并重启网关，资料未进入模型分析', None
                    if reply.status_code == 502:
                        data = bytearray()
                        for part in reply.iter_bytes():
                            data.extend(part)
                            if len(data) > 4096:
                                break
                        try:
                            detail = json.loads(data).get("detail", {}) if len(data) <= 4096 else {}
                            if isinstance(detail, dict) and detail.get("code") == "invalid_provider_result":
                                return "failed", "模型输出未通过校验，结果未发布；本次可能已产生用量，不会自动重试", None
                        except (ValueError, TypeError, AttributeError):
                            pass
                    status = "failed" if 400 <= reply.status_code < 500 else "unknown"
                    message = ("AI 网关拒绝了请求，请检查配置后手动重试" if status == "failed" else
                               "AI 请求结果未知，可能已经产生用量；请确认后再手动重试")
                    return status, message, None
                content = bytearray()
                for part in reply.iter_bytes():
                    content.extend(part)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response too large")
        response = GatewayResponse.model_validate_json(content)
        validate_result(request, response.result)
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unknown", "AI 请求结果未知，可能已经产生用量；请确认后再手动重试", None
    except (ValueError, TypeError):
        return "failed", "AI 返回结果未通过结构或证据校验，请手动重新分析", None
    return "completed", None, response
