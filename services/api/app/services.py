from __future__ import annotations

import hashlib
import itertools
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.orm import Session

from .auth import token_hash
from .config import settings
from .errors import ApiError
from .article_links import article_url
from .models import (
    AgentConnection,
    AuditEvent,
    ConnectionGrant,
    DeletionJob,
    Domain,
    DomainTrash,
    EvidenceChunk,
    ExperienceRecord,
    FileAsset,
    DocumentJob,
    IdempotencyRecord,
    OCRJob,
    PersonalContext,
    Project,
    Source,
    SourceProject,
    SourceTag,
    SourceRevision,
    SourceSearchTerm,
    SourceSearchDocument,
    WebReadJob,
    WebReadPreference,
    WebReadMarkdown,
    FeishuDocumentArtifact,
    SourceImage,
    utcnow,
)
from .schemas import CaptureCreate, ConnectionCreate, ConnectionUpdate, ContextPatch, ExperienceCreate, SearchRequest


def canonicalize_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    tracking = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking
    ]
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and not ((parts.scheme == "http" and parts.port == 80) or (parts.scheme == "https" and parts.port == 443)):
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def payload_digest(payload: CaptureCreate) -> str:
    body = payload.model_dump(mode="json", exclude_none=True)
    if not payload.feishu_images:
        body.pop("feishu_images", None)  # Preserve hashes of pre-image clients.
    for image in body.get("feishu_images", []):
        if image.get("retrieval_method") == "download":
            image.pop("retrieval_method", None)  # Preserve pre-preview client hashes.
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_domain(db: Session, owner_id: str, domain_id: str) -> Domain:
    domain = db.scalar(select(Domain).where(Domain.id == domain_id, Domain.owner_id == owner_id))
    if domain is None or db.get(DomainTrash, domain_id) is not None:
        raise ApiError(404, "not_found", "知识域不存在")
    return domain


def require_project(db: Session, owner_id: str, project_id: str | None) -> Project | None:
    if project_id is None:
        return None
    project = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == owner_id))
    if project is None:
        raise ApiError(404, "not_found", "项目不存在")
    return project


def require_source(db: Session, owner_id: str, source_id: str) -> Source:
    source = db.scalar(
        select(Source).where(Source.id == source_id, Source.owner_id == owner_id, Source.deleted_at.is_(None))
    )
    if source is None:
        raise ApiError(404, "not_found", "资料不存在")
    return source


def content_type(source: Source) -> str:
    if source.kind == "file":
        return "file"
    if source.kind in {"url", "browser_page", "feishu_document"}:
        return "link"
    return "note"


def _create_revision(db: Session, source: Source, text: str) -> SourceRevision:
    version = db.scalar(select(func.max(SourceRevision.version)).where(SourceRevision.source_id == source.id)) or 0
    revision = SourceRevision(source_id=source.id, version=version + 1, extracted_text=text)
    db.add(revision)
    db.flush()
    if text:
        db.add(
            EvidenceChunk(
                revision_id=revision.id,
                ordinal=0,
                text=text,
                locator_kind="text",
                locator_start=0,
                locator_end=len(text),
            )
        )
    source.current_revision_id = revision.id
    return revision


def create_capture(db: Session, owner_id: str, key: str, payload: CaptureCreate) -> tuple[Source, bool]:
    if not key or len(key) > 200:
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key 必填且不能超过 200 字符")
    limit = (settings.max_feishu_capture_chars if payload.capture_type == "feishu_document"
             else settings.max_capture_chars)
    if len(payload.content.text) > limit:
        raise ApiError(413, "capture_too_large", "采集文本超过当前限制")
    ocr_sections = [f"图片 {image.ordinal + 1}（原文块 {image.block_id}）OCR 识别文字：\n{image.ocr_text}"
                    for image in payload.feishu_images if image.ocr_text.strip()]
    if len(payload.content.text) + sum(map(len, ocr_sections)) + len(ocr_sections) * 2 > limit:
        raise ApiError(413, "capture_too_large", "正文及图片识别文字超过当前限制")
    if payload.content.image_urls and payload.capture_type != 'browser_page':
        raise ApiError(422, "images_not_supported", "图片链接仅支持浏览器整页采集", False)

    require_domain(db, owner_id, payload.context.domain_id)
    require_project(db, owner_id, payload.context.project_id)
    digest = payload_digest(payload)
    existing = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.owner_id == owner_id,
            IdempotencyRecord.client_request_id == key,
        )
    )
    if existing:
        if existing.payload_hash != digest:
            raise ApiError(409, "idempotency_conflict", "相同幂等键已用于不同请求")
        source = db.get(Source, existing.source_id)
        if source is None or source.deleted_at is not None:
            raise ApiError(409, "idempotency_result_deleted", "该幂等请求的原结果已删除")
        return source, True

    raw_url = str(payload.source_url) if payload.source_url else None
    title = payload.title or (raw_url if raw_url else payload.content.text.strip()[:80]) or "未命名资料"
    source = Source(
        owner_id=owner_id,
        domain_id=payload.context.domain_id,
        kind=payload.capture_type,
        title=title,
        raw_input=payload.content.text or raw_url,
        raw_url=raw_url,
        canonical_url=canonicalize_url(raw_url),
        visibility="app_and_agents",
        coverage=payload.coverage,
        processing_status="pending" if payload.capture_type == "url" and payload.coverage == "link_only"
                          else "needs_input" if payload.coverage == "link_only" else "usable",
        captured_at=payload.captured_at,
        client_kind=payload.client.kind,
        client_version=payload.client.version,
    )
    db.add(source)
    db.flush()
    evidence_text = payload.content.text.strip()
    if ocr_sections:
        evidence_text += "\n\n" + "\n\n".join(ocr_sections)
    if not evidence_text and raw_url:
        # This records exactly what the user submitted; coverage remains link_only.
        evidence_text = raw_url
    revision = _create_revision(db, source, evidence_text)
    if payload.content.image_urls:
        from .web_images import BROWSER_IMAGE_METHOD, enqueue_images
        # Keep user text separate so Markdown-like prose cannot add download jobs.
        urls = list(dict.fromkeys(str(url) for url in payload.content.image_urls))
        markdown = '\n\n'.join(f'![图片 {i + 1}](<{url}>)' for i, url in enumerate(urls))
        revision.parser_version = 'browser-images-v1'
        db.add(WebReadMarkdown(revision_id=revision.id, markdown=markdown))
        enqueue_images(db, source, revision, markdown, BROWSER_IMAGE_METHOD)
    if payload.feishu_document is not None:
        revision.parser_version = "feishu-cli-v1"
        db.add(WebReadMarkdown(revision_id=revision.id, markdown=payload.content.text))
        db.add(FeishuDocumentArtifact(
            revision_id=revision.id, document_id=payload.feishu_document.document_id,
            document_revision_id=payload.feishu_document.revision_id,
            cli_version=payload.feishu_document.cli_version))
        for image in payload.feishu_images:
            if db.get(SourceImage, str(image.id)) is not None:
                raise ApiError(409, "image_id_conflict", "图片 ID 已被其他采集使用")
            db.add(SourceImage(id=str(image.id), source_id=source.id, revision_id=revision.id,
                               ordinal=image.ordinal, block_id=image.block_id, caption=image.caption,
                               status=image.status, failure_reason=image.failure_reason, ocr_text=image.ocr_text,
                               retrieval_method=image.retrieval_method))
    if source.kind == "url" and source.coverage == "link_only":
        preference = db.get(WebReadPreference, owner_id)
        db.add(WebReadJob(owner_id=owner_id, source_id=source.id,
                          engine=preference.engine if preference else "legacy",
                          cleaning_enabled=bool(preference and preference.engine == "crawl4ai"
                                                and preference.cleaning_enabled)))
    db.add(
        PersonalContext(
            source_id=source.id,
            reason=payload.context.save_reason,
            notes=payload.context.note,
            applicability=None,
        )
    )
    if payload.context.project_id:
        db.add(SourceProject(source_id=source.id, project_id=payload.context.project_id))
    db.add(
        IdempotencyRecord(
            owner_id=owner_id,
            client_request_id=key,
            payload_hash=digest,
            source_id=source.id,
        )
    )
    db.flush()
    reindex_source(db, source)
    db.commit()
    db.refresh(source)
    return source, False


def patch_context(db: Session, owner_id: str, source: Source, patch: ContextPatch) -> Source:
    if patch.domain_id is not None:
        require_domain(db, owner_id, patch.domain_id)
        source.domain_id = patch.domain_id
    if patch.visibility is not None:
        source.visibility = patch.visibility
    context = db.get(PersonalContext, source.id)
    if context is None:
        context = PersonalContext(source_id=source.id, version=1)
        db.add(context)
    if patch.version is not None and patch.version != context.version:
        raise ApiError(409, "context_version_conflict", "个人上下文已被更新，请刷新后重试")
    if patch.note is not None:
        context.notes = patch.note
    if patch.save_reason is not None:
        context.reason = patch.save_reason
    if patch.applicability is not None:
        context.applicability = patch.applicability
    if "project_ids" in patch.model_fields_set:
        project_ids = patch.project_ids or []
        project_id = project_ids[0] if project_ids else None
        require_project(db, owner_id, project_id)
        db.execute(delete(SourceProject).where(SourceProject.source_id == source.id))
        if project_id:
            db.add(SourceProject(source_id=source.id, project_id=project_id))
    elif patch.project_id is not None:
        require_project(db, owner_id, patch.project_id)
        db.execute(delete(SourceProject).where(SourceProject.source_id == source.id))
        db.add(SourceProject(source_id=source.id, project_id=patch.project_id))
    changed_fields = patch.model_fields_set - {"version"}
    if changed_fields:
        context.version += 1
        if {"note", "save_reason", "applicability"} & changed_fields:
            db.flush()
            reindex_source(db, source)
    db.commit()
    db.refresh(source)
    return source


def add_experience(
    db: Session, owner_id: str, source: Source, payload: ExperienceCreate
) -> ExperienceRecord:
    require_project(db, owner_id, payload.project_id)
    record = ExperienceRecord(
        owner_id=owner_id,
        source_id=source.id,
        project_id=payload.project_id,
        result=payload.result,
        notes=payload.notes,
        verified_at=payload.verified_at or utcnow(),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def current_web_base_revision(db: Session, source: Source,
                              current: SourceRevision | None = None) -> SourceRevision | None:
    current = current or (db.get(SourceRevision, source.current_revision_id)
                          if source.current_revision_id else None)
    if current is None:
        return None
    return db.scalar(select(SourceRevision).where(
        SourceRevision.source_id == source.id,
        SourceRevision.version <= current.version,
        SourceRevision.parser_version.in_(["web-html-v1", "web-crawl4ai-v1", "web-wechat-v1", "feishu-cli-v1", "browser-images-v1"]),
    ).order_by(SourceRevision.version.desc()).limit(1))


def current_web_markdown(db: Session, source: Source,
                         revision: SourceRevision | None = None) -> tuple[str, str] | None:
    base = current_web_base_revision(db, source, revision)
    if base is None:
        return None
    if base.parser_version == 'browser-images-v1':
        artifact = db.get(WebReadMarkdown, base.id)
        return (base.extracted_text + '\n\n' + artifact.markdown, 'browser_extension') if artifact else None
    if base.parser_version in {"web-crawl4ai-v1", "web-wechat-v1", "feishu-cli-v1"}:
        artifact = db.get(WebReadMarkdown, base.id)
        origin = "wechat_ua" if base.parser_version == "web-wechat-v1" else ("feishu_cli" if base.parser_version == "feishu-cli-v1" else "crawl4ai")
        return (artifact.markdown, origin) if artifact else None
    if base.parser_version != "web-html-v1":
        return None
    last_completed = db.scalar(select(WebReadJob).where(
        WebReadJob.source_id == source.id, WebReadJob.status == "completed",
    ).order_by(WebReadJob.completed_at.desc(), WebReadJob.id.desc()).limit(1))
    if last_completed is None or last_completed.engine != "crawl4ai":
        return None
    body = db.scalar(select(EvidenceChunk.text).where(
        EvidenceChunk.revision_id == base.id, EvidenceChunk.ordinal == 0))
    if not body:
        return None
    return f"# {source.title.strip()}\n\n{body.strip()}", "html_fallback"


def reading_text(db: Session, source: Source, revision: SourceRevision | None) -> str:
    """Keep image OCR in search evidence, but omit its exact generated suffix in the reader."""
    if revision is None:
        return ""
    text = revision.extracted_text
    base = current_web_base_revision(db, source, revision)
    if base and base.parser_version == 'browser-images-v1' and text.startswith(base.extracted_text):
        images = db.scalars(select(SourceImage).where(SourceImage.revision_id == base.id)
                            .order_by(SourceImage.ordinal)).all()
        artifact = db.get(WebReadMarkdown, base.id)
        if artifact:
            from .web_images import local_image_markdown
            return base.extracted_text + '\n\n' + local_image_markdown(artifact.markdown, images) + text[len(base.extracted_text):]
    if base and base.parser_version == 'web-wechat-v1' and text.startswith(base.extracted_text):
        images = db.scalars(select(SourceImage).where(SourceImage.revision_id == base.id)
                            .order_by(SourceImage.ordinal)).all()
        markdown = db.get(WebReadMarkdown, base.id)
        if images and markdown:
            from .web_images import local_image_markdown
            return local_image_markdown(markdown.markdown, images) + text[len(base.extracted_text):]
    if source.kind != "feishu_document":
        return text
    if base is None or not text.startswith(base.extracted_text):
        return text
    images = db.scalars(select(SourceImage).where(SourceImage.revision_id == base.id)
                        .order_by(SourceImage.ordinal)).all()
    sections = [f"图片 {image.ordinal + 1}（原文块 {image.block_id}）OCR 识别文字：\n{image.ocr_text}"
                for image in images if image.ocr_text.strip()]
    suffix = "\n\n" + "\n\n".join(sections) if sections else ""
    if not suffix or not base.extracted_text.endswith(suffix):
        return text
    return base.extracted_text[:-len(suffix)] + text[len(base.extracted_text):]


def source_view(db: Session, source: Source, detail: bool = False) -> dict:
    from .knowledge_tags import source_tag_views
    context = db.get(PersonalContext, source.id)
    project_ids = list(
        db.scalars(select(SourceProject.project_id).where(SourceProject.source_id == source.id)).all()
    )
    revision = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    result = {
        "id": source.id,
        "title": source.title,
        "capture_type": source.kind,
        "content_type": content_type(source),
        "source_url": source.raw_url,
        "domain_id": source.domain_id,
        "project_ids": project_ids,
        **source_tag_views(db, source),
        "visibility": source.visibility,
        "coverage": source.coverage,
        "upload_status": source.upload_status,
        "processing_status": source.processing_status,
        "revision": revision.version if revision else None,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
        "context": {
            "note": context.notes if context else None,
            "save_reason": context.reason if context else None,
            "applicability": context.applicability if context else None,
            "version": context.version if context else 0,
        },
    }
    if detail:
        asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source.id))
        ocr_job = db.scalar(select(OCRJob).where(OCRJob.source_id == source.id)
                            .order_by(OCRJob.created_at.desc(), OCRJob.id.desc()).limit(1))
        document_job = db.scalar(select(DocumentJob).where(DocumentJob.source_id == source.id)
                                 .order_by(DocumentJob.created_at.desc(), DocumentJob.id.desc()).limit(1))
        result["file"] = ({"filename": asset.filename, "size_bytes": asset.size_bytes,
                           "sha256": asset.sha256, "download_path": f"/v1/sources/{source.id}/file"}
                          if asset else None)
        from .runtime_extensions import job_status, job_message
        result["ocr"] = ({"model": ocr_job.model, "engine": ocr_job.engine, "status": job_status(ocr_job),
                          "last_error": job_message(ocr_job), "page_count": ocr_job.page_count}
                         if ocr_job else None)
        result["document_parse"] = ({"status": job_status(document_job),
                                     "last_error": job_message(document_job)}
                                    if document_job else None)
        web_job = db.scalar(select(WebReadJob).where(WebReadJob.source_id == source.id)
                            .order_by(WebReadJob.created_at.desc(), WebReadJob.id.desc()).limit(1))
        result["web_read"] = ({"status": web_job.status, "last_error": web_job.last_error,
                               "engine": web_job.engine, "cleaning_enabled": web_job.cleaning_enabled,
                               "attempts": web_job.attempts,
                               "created_at": web_job.created_at}
                              if web_job else None)
        web_markdown = current_web_markdown(db, source, revision)
        result["crawl4ai_markdown_available"] = web_markdown is not None
        result["crawl4ai_markdown_origin"] = web_markdown[1] if web_markdown else None
        result['web_images_can_archive'] = False
        if web_markdown and web_markdown[1] in {'wechat_ua', 'browser_extension'}:
            from .web_images import image_references
            try:
                result['web_images_can_archive'] = bool(image_references(web_markdown[0]))
            except ValueError:
                result['web_images_can_archive'] = True
        base = current_web_base_revision(db, source, revision)
        result['browser_image_urls'] = []
        if base and base.parser_version == 'browser-images-v1':
            from .web_images import image_references
            artifact = db.get(WebReadMarkdown, base.id)
            result['browser_image_urls'] = [item.url for item in image_references(artifact.markdown)] if artifact else []
        feishu = db.get(FeishuDocumentArtifact, base.id) if base else None
        result["feishu_document"] = ({"document_id": feishu.document_id,
                                      "revision_id": feishu.document_revision_id,
                                      "cli_version": feishu.cli_version} if feishu else None)
        result["images"] = [{"id": image.id, "ordinal": image.ordinal, "block_id": image.block_id,
                             "caption": image.caption, "status": image.status,
                             "failure_reason": image.failure_reason, "ocr_text": image.ocr_text,
                             "retrieval_method": image.retrieval_method,
                             "width": image.width, "height": image.height, "size_bytes": image.size_bytes,
                             "download_path": f"/v1/sources/{source.id}/images/{image.id}"
                                              if image.status == "saved" else None}
                            for image in db.scalars(select(SourceImage).where(
                                SourceImage.source_id == source.id,
                                SourceImage.revision_id == (base.id if base else source.current_revision_id)
                            ).order_by(SourceImage.ordinal))]
        evidence = []
        if revision:
            for item in db.scalars(
                select(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id).order_by(EvidenceChunk.ordinal)
            ):
                evidence.append(evidence_view(item, revision))
        experiences = [
            experience_view(item)
            for item in db.scalars(
                select(ExperienceRecord)
                .where(ExperienceRecord.source_id == source.id)
                .order_by(ExperienceRecord.verified_at.desc())
            )
        ]
        result.update(
            {
                "raw_input": source.raw_input,
                "extracted_text": revision.extracted_text if revision else "",
                "reading_text": reading_text(db, source, revision),
                "evidence": evidence,
                "experiences": experiences,
            }
        )
    return result


def source_list_views(db: Session, sources: list[Source]) -> list[dict]:
    """Build a source page with bounded queries and without loading revision bodies."""
    if not sources:
        return []
    ids = [source.id for source in sources]
    contexts = {item.source_id: item for item in db.scalars(
        select(PersonalContext).where(PersonalContext.source_id.in_(ids))
    )}
    projects: dict[str, list[str]] = {source_id: [] for source_id in ids}
    for source_id, project_id in db.execute(
        select(SourceProject.source_id, SourceProject.project_id)
        .where(SourceProject.source_id.in_(ids))
    ):
        projects[source_id].append(project_id)
    from .knowledge_tags import source_tag_views_many
    tag_views = source_tag_views_many(db, sources)
    revision_ids = [source.current_revision_id for source in sources if source.current_revision_id]
    versions = dict(db.execute(
        select(SourceRevision.id, SourceRevision.version)
        .where(SourceRevision.id.in_(revision_ids))
    ).all()) if revision_ids else {}
    items = []
    for source in sources:
        context = contexts.get(source.id)
        items.append({
            "id": source.id,
            "title": source.title,
            "capture_type": source.kind,
            "content_type": content_type(source),
            "preview": (source.raw_input or "")[:120] if content_type(source) == "note" else None,
            "source_url": source.raw_url,
            "domain_id": source.domain_id,
            "project_ids": projects[source.id],
            **tag_views[source.id],
            "visibility": source.visibility,
            "coverage": source.coverage,
            "upload_status": source.upload_status,
            "processing_status": source.processing_status,
            "revision": versions.get(source.current_revision_id),
            "created_at": source.created_at,
            "updated_at": source.updated_at,
            "context": {
                "note": context.notes if context else None,
                "save_reason": context.reason if context else None,
                "applicability": context.applicability if context else None,
                "version": context.version if context else 0,
            },
        })
    return items


def evidence_view(item: EvidenceChunk, revision: SourceRevision) -> dict:
    return {
        "evidence_id": item.id,
        "revision": revision.version,
        "text": item.text,
        "locator": {
            "kind": item.locator_kind,
            "start": item.locator_start,
            "end": item.locator_end,
        },
    }


def experience_view(item: ExperienceRecord) -> dict:
    return {
        "id": item.id,
        "source_id": item.source_id,
        "project_id": item.project_id,
        "result": item.result,
        "notes": item.notes,
        "verified_at": item.verified_at,
    }


def create_connection(db: Session, owner_id: str, payload: ConnectionCreate) -> tuple[AgentConnection, str]:
    unique_domains = list(dict.fromkeys(payload.domain_ids))
    for domain_id in unique_domains:
        require_domain(db, owner_id, domain_id)
    token = f"pka_{secrets.token_urlsafe(32)}"
    connection = AgentConnection(
        owner_id=owner_id,
        client_name=payload.client_name,
        token_hash=token_hash(token),
        expires_at=utcnow() + timedelta(days=payload.expires_in_days),
    )
    db.add(connection)
    db.flush()
    for domain_id in unique_domains:
        db.add(ConnectionGrant(connection_id=connection.id, domain_id=domain_id))
    from .agent_connections import supersede_others
    supersede_others(db, connection)
    db.commit()
    db.refresh(connection)
    return connection, token


def update_connection(db: Session, owner_id: str, connection_id: str, payload: ConnectionUpdate) -> None:
    from .agent_connections import is_current, supersede_others
    connection = db.scalar(select(AgentConnection).where(
        AgentConnection.id == connection_id, AgentConnection.owner_id == owner_id))
    if connection is None:
        raise ApiError(404, "not_found", "Agent 连接不存在")
    expires = connection.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if connection.status != "active" or connection.revoked_at or expires <= utcnow() or not is_current(db, connection):
        raise ApiError(409, "inactive_connection", "连接已失效，请刷新后重新接入")
    domains = list(dict.fromkeys(payload.domain_ids))
    for domain_id in domains:
        require_domain(db, owner_id, domain_id)
    db.execute(delete(ConnectionGrant).where(ConnectionGrant.connection_id == connection.id))
    for domain_id in domains:
        db.add(ConnectionGrant(connection_id=connection.id, domain_id=domain_id))
    supersede_others(db, connection)
    db.commit()


def revoke_connection(db: Session, owner_id: str, connection_id: str) -> None:
    connection = db.scalar(
        select(AgentConnection).where(
            AgentConnection.id == connection_id, AgentConnection.owner_id == owner_id
        )
    )
    if connection is None:
        raise ApiError(404, "not_found", "Agent 连接不存在")
    connection.status = "revoked"
    connection.revoked_at = utcnow()
    db.commit()


def _query_tokens(value: str) -> list[str]:
    lowered = value.casefold()
    ascii_tokens = re.findall(r"[a-z0-9_+.#-]+", lowered)
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    cjk_tokens: list[str] = []
    for run in cjk_runs:
        if len(run) == 1:
            cjk_tokens.append(run)
        else:
            cjk_tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(ascii_tokens + cjk_tokens))


def _identifier_matches(value: str) -> dict[str, list[tuple[int, int]]]:
    """Complete ASCII codes/versions, with Chinese and punctuation as separators."""
    result: dict[str, list[tuple[int, int]]] = {}
    for match in re.finditer(r"[a-z0-9]+(?:[_.+#-][a-z0-9]+)*", value, flags=re.IGNORECASE | re.ASCII):
        token = match.group().casefold()
        mixed = re.search(r"[a-z]", token) and re.search(r"[0-9]", token)
        version = re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", token)
        if mixed or version:
            result.setdefault(token, []).append(match.span())
    return result


def _best_keyword_span(text: str, query: str, tokens: list[str]) -> tuple[int, int] | None:
    """Locate the tightest passage covering the most query terms in one evidence block."""
    identifiers = set(_identifier_matches(query))
    identifier_spans = _identifier_matches(text) if identifiers else {}
    exact = re.search(re.escape(query.strip()), text, flags=re.IGNORECASE) if query.strip() else None
    if exact and any(not any(start >= exact.start() and end <= exact.end() for start, end in identifier_spans.get(token, []))
                     for token in identifiers):
        exact = None
    if exact:
        return exact.span()
    hits: list[tuple[int, int, int]] = []
    for index, token in enumerate(dict.fromkeys(tokens)):
        spans = identifier_spans.get(token, []) if token in identifiers else [
            match.span() for match in re.finditer(re.escape(token), text, flags=re.IGNORECASE)]
        hits.extend((start, end, index) for start, end in spans)
    if not hits:
        return None
    hits.sort()
    counts = [0] * len(dict.fromkeys(tokens))
    left = 0
    best_span = (hits[0][0], hits[0][1])
    best_score = (0, 0, 0, 0)
    for right, (start, end, token_index) in enumerate(hits):
        counts[token_index] += 1
        while left < right and end - hits[left][0] > 160:
            counts[hits[left][2]] -= 1
            left += 1
        span = (hits[left][0], max(hit[1] for hit in hits[left:right + 1]))
        identifier_count = sum(count > 0 for count, token in zip(counts, dict.fromkeys(tokens)) if token in identifiers)
        score = (identifier_count, sum(count > 0 for count in counts), -(span[1] - span[0]), -span[0])
        if score > best_score:
            best_score = score
            best_span = span
    return best_span


def reindex_source(db: Session, source: Source) -> None:
    """Keep a portable, owner-scoped inverted index for the current revision."""
    document = db.scalar(select(SourceSearchDocument).where(SourceSearchDocument.source_id == source.id))
    if document is None:
        document = SourceSearchDocument(source_id=source.id, owner_id=source.owner_id)
        db.add(document)
        db.flush()
    else:
        document.owner_id = source.owner_id
        db.execute(delete(SourceSearchTerm).where(SourceSearchTerm.document_id == document.id))
    revision = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    context = db.get(PersonalContext, source.id)
    parts = (
        source.title,
        revision.extracted_text if revision else "",
        context.reason if context else "",
        context.notes if context else "",
        context.applicability if context else "",
    )
    terms: set[str] = set()
    for part in parts:
        if not part:
            continue
        lowered = part.casefold()
        terms.update(match.group() for match in re.finditer(r"[a-z0-9_+.#-]+", lowered)
                     if len(match.group()) <= 128)
        for match in re.finditer(r"[\u3400-\u9fff]+", lowered):
            run = match.group()
            terms.update(run)
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
    # Preserve common prefix searches without scanning every source body.
    terms.update(token[:length] for token in tuple(terms)
                 if token.isascii() and len(token) > 2
                 for length in range(2, min(len(token), 32) + 1))
    terms.add("__indexed__")
    rows = ({"document_id": document.id, "term": term}
            for term in terms)
    while chunk := list(itertools.islice(rows, 1000)):
        db.execute(insert(SourceSearchTerm), chunk)
    from .tags import replace_auto_tags
    replace_auto_tags(db, source)
    from .semantic import enqueue_semantic_index
    enqueue_semantic_index(db, source)


def ensure_search_index(db: Session) -> None:
    """Backfill sources created before this index existed, in bounded batches."""
    while True:
        missing = db.scalars(
            select(Source)
            .outerjoin(SourceSearchDocument, and_(
                SourceSearchDocument.source_id == Source.id,
                SourceSearchDocument.owner_id == Source.owner_id,
            ))
            .outerjoin(SourceSearchTerm, and_(
                SourceSearchTerm.document_id == SourceSearchDocument.id,
                SourceSearchTerm.term == "__indexed__",
            ))
            .where(Source.deleted_at.is_(None), SourceSearchTerm.document_id.is_(None))
            .limit(100)
        ).all()
        if not missing:
            return
        for source in missing:
            reindex_source(db, source)
        db.commit()


def search_knowledge(
    db: Session,
    owner_id: str,
    request: SearchRequest,
    allowed_domains: set[str] | None = None,
    for_agent: bool = False,
    *,
    allowed_source_ids: frozenset[str] | None = None,
) -> list[dict]:
    if request.domain_ids:
        requested = set(request.domain_ids)
        for domain_id in requested:
            require_domain(db, owner_id, domain_id)
        domains = requested if allowed_domains is None else requested & allowed_domains
    else:
        domains = allowed_domains
    if request.project_id:
        require_project(db, owner_id, request.project_id)
    scope = select(Source).where(Source.owner_id == owner_id, Source.deleted_at.is_(None),
        Source.processing_status.not_in(("deleting", "deleted")))
    if allowed_source_ids is not None:
        scope = scope.where(Source.id.in_(allowed_source_ids))
    if request.source_id is not None:
        scope = scope.where(Source.id == request.source_id)
    if domains is not None:
        scope = scope.where(Source.domain_id.in_(domains))
    if request.project_id:
        scope = scope.where(Source.id.in_(select(SourceProject.source_id).where(SourceProject.project_id == request.project_id)))
    if for_agent:
        scope = scope.where(Source.visibility != "app_only")
    from .summary_retrieval import filter_tags, tag_hits, summary_hits
    scope = filter_tags(db, owner_id, request, scope, for_agent)
    if request.mode == "exact":
        return exact_search(db, scope, request)
    source_ids = scope.with_only_columns(Source.id)
    search_text = request.query
    original_tokens = _query_tokens(search_text)
    identifiers = set(_identifier_matches(search_text))
    tokens = list(dict.fromkeys(token.strip(".+#-") if token.strip(".+#-") in identifiers else token
                               for token in original_tokens))
    # Existing indexes store ASCII prefixes up to 32 characters. A long code
    # followed by punctuation may lack its unpunctuated full term; use its
    # indexed prefix as a candidate and verify the complete token in originals.
    identifier_candidates = set(identifiers)
    identifier_candidates.update(token[:32] for token in identifiers if len(token) > 32)
    indexed_tokens = [token for token in dict.fromkeys([*original_tokens[:64], *sorted(identifier_candidates)])
                      if len(token) <= 128]
    stmt = scope
    if indexed_tokens:
        # Resolve compact term candidates first, then apply cached and real
        # Source Owner gates. The outer scope retains Domain/private/tag gates.
        document_ids = select(SourceSearchTerm.document_id).where(SourceSearchTerm.term.in_(indexed_tokens))
        stmt = stmt.where(Source.id.in_(select(SourceSearchDocument.source_id).where(
            SourceSearchDocument.owner_id == owner_id, SourceSearchDocument.id.in_(document_ids))))
    ranked = []
    exact_identifiers: dict[str, int] = {}
    for source in db.scalars(stmt):
        revision = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
        if revision is None:
            continue
        context = db.get(PersonalContext, source.id)
        context_text = " ".join(item for item in [context.reason if context else None,
            context.notes if context else None, context.applicability if context else None] if item)
        title_identifiers = set(_identifier_matches(source.title)) if identifiers else set()
        context_identifiers = set(_identifier_matches(context_text)) if identifiers else set()
        for evidence in db.scalars(select(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id)):
            haystack = f"{source.title} {evidence.text} {context_text}".casefold()
            evidence_identifiers = set(_identifier_matches(evidence.text)) if identifiers else set()
            original_identifiers = evidence_identifiers | title_identifiers
            all_identifiers = original_identifiers | context_identifiers
            matches = identifiers & original_identifiers
            score = 0
            for token in tokens:
                present = token in all_identifiers if token in identifiers else token in haystack
                if present:
                    in_evidence = token in evidence_identifiers if token in identifiers else token in evidence.text.casefold()
                    score += 2 if in_evidence else 1
            if request.query.casefold() in haystack and identifiers.issubset(all_identifiers):
                score += 5
            if score:
                # Prefer the actual identifier-bearing block before common prose.
                ranked.append((len(matches), len(identifiers & evidence_identifiers), float(score), source, evidence, revision))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3].created_at, item[3].id))
    keyword = {}
    for exact_count, _, score, source, evidence, revision in ranked:
        keyword.setdefault(source.id, (source, evidence, revision))
        exact_identifiers.setdefault(source.id, exact_count)
        if len(keyword) >= 60:
            break
    from .runtime_extensions import available
    if not available("semantic"):
        raise ApiError(409, "extension_required", "语义搜索扩展未安装或未启用，请在设置→扩展功能中安装后重新打开客户端")
    from . import semantic as semantic_module
    cached_vector, embedding_error = [], []
    def query_vector():
        if embedding_error:
            raise embedding_error[0]
        if not cached_vector:
            try:
                cached_vector.append(semantic_module.embed_query(request.query))
            except Exception as exc:
                embedding_error.append(exc)
                raise
        return cached_vector[0]
    passage_options = {"distinct_sources": False} if request.result_unit == "passage" else {}
    semantic_rows = semantic_module.semantic_hits(
        db, owner_id, request.query, domains, request.project_id, for_agent,
        allowed_source_ids=source_ids, query_vector=query_vector, **passage_options)
    semantic = {}
    for row in semantic_rows:
        semantic.setdefault(row[1].id, row)
    summary_keyword, summary_vectors = summary_hits(db, owner_id, request.query, tokens, scope, query_vector)
    tagged = tag_hits(db, owner_id, request.query, tokens, scope)
    from .domain_retrieval import node_hits
    nodes = node_hits(db, owner_id, request.query, tokens, scope, for_agent, query_vector=query_vector)
    # Short questions often name a category rather than an exact word in the note.
    # Only offer bounded, explicitly uncertain original-text candidates on no hits.
    fallback = False
    if not any((keyword, semantic, summary_keyword, summary_vectors, tagged)) and len(request.query.strip()) <= 20:
        fallback_rows = semantic_module.semantic_hits(
            db, owner_id, request.query, domains, request.project_id, for_agent, limit=3,
            allowed_source_ids=source_ids, query_vector=query_vector,
            min_similarity=max(0.35, settings.semantic_min_similarity - 0.15), **passage_options)
        semantic = {}
        for row in fallback_rows:
            semantic.setdefault(row[1].id, row)
        fallback = bool(semantic)
        if fallback:
            semantic_rows = fallback_rows
    channels = [keyword, semantic, summary_keyword, summary_vectors, tagged]
    ranks = [{source_id: rank for rank, source_id in enumerate(channel, 1)} for channel in channels]
    combined = set().union(*(set(channel) for channel in channels))
    scores = {source_id: sum(1 / (60 + ranking[source_id]) for ranking in ranks if source_id in ranking) for source_id in combined}
    # Identifier priority only comes from current title/original evidence, never
    # from generated summaries, tags, context or hierarchy navigation hints.
    ordered = sorted(combined, key=lambda source_id: (-exact_identifiers.get(source_id, 0), -scores[source_id], source_id))
    # Preserve ordinary-query fusion and the evidence budget. Nodes only add
    # missing candidates after the original/document-summary/tag channels.
    for rank, source_id in enumerate(nodes, 1):
        if source_id not in combined:
            scores[source_id] = 1 / (60 + rank)
            ordered.append(source_id)
    results = []
    for source_id in ordered[:request.limit]:
        lexical, matched = keyword.get(source_id), semantic.get(source_id)
        summarized = summary_keyword.get(source_id) or summary_vectors.get(source_id)
        if lexical is not None:
            source, evidence, revision = lexical
            original_start = None
        elif matched is not None:
            _, source, evidence, revision, embedding = matched
            original_start = embedding.start
        elif summarized:
            source, artifact, (evidence, revision, original_start), text, summary_start = summarized
        elif source_id in tagged:
            source, evidence, revision = tagged[source_id]
            original_start = 0
        else:
            source, evidence, revision, original_start, _ = nodes[source_id]
        if original_start is None:
            span = _best_keyword_span(evidence.text, request.query, tokens)
            match_start, match_end = span if span else (0, 0)
            start = max(0, match_start - 100)
            end = min(len(evidence.text), max(match_end + 100, start + min(settings.evidence_snippet_chars, 300)))
            snippet = evidence.text[start:end]
            locator = {"kind": evidence.locator_kind,
                "start": evidence.locator_start + match_start if evidence.locator_kind == "text" else evidence.locator_start,
                "end": evidence.locator_start + match_end if evidence.locator_kind == "text" else evidence.locator_end}
        else:
            end = min(len(evidence.text), original_start + settings.evidence_snippet_chars)
            snippet = evidence.text[original_start:end]
            locator = {"kind": evidence.locator_kind,
                "start": evidence.locator_start + original_start if evidence.locator_kind == "text" else evidence.locator_start,
                "end": evidence.locator_start + end if evidence.locator_kind == "text" else evidence.locator_end}
        item = search_item_view(db, source, evidence, revision, scores[source_id], snippet, locator)
        has_keyword = bool(lexical or source_id in summary_keyword or source_id in tagged
            or (source_id in nodes and nodes[source_id][-1]["match_type"] != "semantic"))
        has_semantic = bool(matched or source_id in summary_vectors
            or (source_id in nodes and nodes[source_id][-1]["match_type"] != "keyword"))
        item["match_type"] = "hybrid" if has_keyword and has_semantic else "semantic" if has_semantic else "keyword"
        item["match_sources"] = (["original"] if lexical or matched else []) + (["summary"] if summarized else []) + (["tag"] if source_id in tagged else [])
        if source_id in nodes:
            item["match_sources"].append("knowledge_node")
            item["knowledge_node"] = nodes[source_id][-1]
            if source_id not in combined:
                item["retrieval_confidence"] = "candidate"
                item["requires_evidence_confirmation"] = True
        if fallback and matched is not None:
            item["retrieval_confidence"] = "candidate"
            item["semantic_similarity"] = matched[0]
            item["requires_evidence_confirmation"] = True
        if summarized:
            _, artifact, _, text, start = summarized
            if source_id in summary_keyword:
                span = _best_keyword_span(text, request.query, tokens)
                start = max(0, span[0] - 100) if span else 0
            item["summary_artifact_id"] = artifact.id
            item["summary_snippet"] = text[start:start + settings.evidence_snippet_chars]
            snapshot = json.loads(artifact.snapshot_json)
            item["summary_coverage"] = snapshot.get("analysis_plan", {}).get("coverage", "partial" if snapshot.get("warnings") else "full")
        results.append(item)
    if request.result_unit == "passage":
        from .search_passages import expand_passages
        return expand_passages(db, request, results, ranked, semantic_rows)
    return results


def exact_search(db: Session, scope, request: SearchRequest) -> list[dict]:
    """Literal title/current-body matches, before limiting, without generated retrieval channels."""
    query = request.query.strip()
    if not query:
        return []
    if request.result_unit == "passage":
        from .search_passages import exact_passages
        return exact_passages(db, scope, request)
    statement = scope.join(SourceRevision, and_(SourceRevision.id == Source.current_revision_id,
        SourceRevision.source_id == Source.id)).add_columns(SourceRevision).where(or_(
            func.lower(Source.title).contains(query.lower(), autoescape=True),
            func.lower(SourceRevision.extracted_text).contains(query.lower(), autoescape=True)))
    statement = statement.order_by(Source.created_at.desc(), Source.id).limit(request.limit)
    items = []
    for source, revision in db.execute(statement):
        match = re.search(re.escape(query), revision.extracted_text, flags=re.IGNORECASE)
        evidence_scope = select(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id)
        evidence = db.scalar(evidence_scope.where(
            func.lower(EvidenceChunk.text).contains(query.lower(), autoescape=True))
            .order_by(EvidenceChunk.ordinal).limit(1)) if match else None
        if evidence is None and match:
            evidence = db.scalar(evidence_scope.where(EvidenceChunk.locator_kind == "text",
                EvidenceChunk.locator_start <= match.start(), EvidenceChunk.locator_end > match.start())
                .order_by(EvidenceChunk.ordinal).limit(1))
        matched_evidence = evidence
        if evidence is None:
            evidence = db.scalar(evidence_scope.order_by(EvidenceChunk.ordinal).limit(1))
        if match:
            start = max(0, match.start() - 70)
            end = min(len(revision.extracted_text), match.end() + 110)
            snippet = revision.extracted_text[start:end]
            locator = {"kind": "text", "start": match.start(), "end": match.end()}
        else:
            snippet = source.title
            locator = {"kind": "title", "start": 0, "end": len(source.title)}
        if evidence is not None:
            item = search_item_view(db, source, evidence, revision, 1.0, snippet=snippet, locator=locator)
        else:
            # A saved attachment can have a title before its body/evidence is parsed.
            from .summary_retrieval import source_tags
            item = {"source_id": source.id, "title": source.title, "capture_type": source.kind,
                    "content_type": content_type(source), "revision": revision.version,
                    "source_url": source.raw_url, "app_url": article_url(db, source),
                    "snippet": snippet, "locator": locator, "score": 1.0, "tags": source_tags(db, source)}
        item["match_sources"] = ["original" if match else "title"]
        if matched_evidence is None:
            item["evidence_id"] = None
        items.append(item)
    return items


def search_item_view(
    db: Session, source: Source, evidence: EvidenceChunk, revision: SourceRevision, score: float,
    snippet: str | None = None, locator: dict | None = None,
) -> dict:
    context = db.get(PersonalContext, source.id)
    latest_experience = db.scalar(
        select(ExperienceRecord)
        .where(ExperienceRecord.source_id == source.id)
        .order_by(ExperienceRecord.verified_at.desc())
        .limit(1)
    )
    snippet = snippet if snippet is not None else evidence.text[: settings.evidence_snippet_chars]
    from .summary_retrieval import source_tags
    return {
        "source_id": source.id,
        "tags": source_tags(db, source),
        "evidence_id": evidence.id,
        "revision": revision.version,
        "title": source.title,
        "capture_type": source.kind,
        "content_type": content_type(source),
        "snippet": snippet,
        "locator": locator or {"kind": evidence.locator_kind, "start": evidence.locator_start, "end": evidence.locator_end},
        "source_url": source.raw_url,
        "app_url": article_url(db, source),
        "coverage": source.coverage,
        "personal_context": {
            "save_reason": context.reason if context else None,
            "note": context.notes if context else None,
            "applicability": context.applicability if context else None,
        },
        "verification": experience_view(latest_experience) if latest_experience else {"result": "untried"},
        "score": score,
    }


def granted_domains(db: Session, connection_id: str) -> set[str]:
    return set(
        db.scalars(
            select(ConnectionGrant.domain_id).where(ConnectionGrant.connection_id == connection_id)
        ).all()
    )


def require_agent_source(
    db: Session, owner_id: str, connection_id: str, source_id: str
) -> Source:
    grants = granted_domains(db, connection_id)
    source = db.scalar(
        select(Source).where(
            Source.id == source_id,
            Source.owner_id == owner_id,
            Source.deleted_at.is_(None),
            Source.visibility != "app_only",
            Source.domain_id.in_(grants),
            Source.processing_status.not_in(["deleting", "deleted"]),
        )
    )
    if source is None:
        raise ApiError(404, "not_found", "资料不存在")
    return source


def agent_evidence(
    db: Session, owner_id: str, connection_id: str, evidence_id: str, context_window: int = 0
) -> dict:
    evidence = db.get(EvidenceChunk, evidence_id)
    if evidence is None:
        raise ApiError(404, "not_found", "证据不存在")
    revision = db.get(SourceRevision, evidence.revision_id)
    if revision is None:
        raise ApiError(404, "not_found", "证据不存在")
    source = require_agent_source(db, owner_id, connection_id, revision.source_id)
    context = db.get(PersonalContext, source.id)
    latest = db.scalar(
        select(ExperienceRecord)
        .where(ExperienceRecord.source_id == source.id)
        .order_by(ExperienceRecord.verified_at.desc())
        .limit(1)
    )
    text = evidence.text
    if context_window and evidence.locator_kind == "text":
        text = revision.extracted_text[
            max(0, evidence.locator_start - context_window) : min(len(revision.extracted_text), evidence.locator_end + context_window)
        ]
    current = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    return {
        "evidence_id": evidence.id,
        "source_id": source.id,
        "title": source.title,
        "app_url": article_url(db, source),
        "revision": revision.version,
        "text": text,
        "locator": {"kind": evidence.locator_kind, "start": evidence.locator_start, "end": evidence.locator_end},
        "personal_context": {
            "save_reason": context.reason if context else None,
            "note": context.notes if context else None,
            "applicability": context.applicability if context else None,
        },
        "verification": experience_view(latest) if latest else {"result": "untried"},
        "has_newer_revision": bool(current and current.version > revision.version),
    }


def record_audit(
    db: Session,
    owner_id: str,
    connection_id: str,
    tool: str,
    result: str,
    source_ids: list[str],
    started: float,
) -> None:
    db.add(
        AuditEvent(
            owner_id=owner_id,
            connection_id=connection_id,
            tool=tool,
            result=result,
            source_ids=",".join(source_ids),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
    )
    db.commit()


def soft_delete_source(db: Session, owner_id: str, source_id: str, *, commit: bool = True) -> DeletionJob:
    source = require_source(db, owner_id, source_id)
    changed = db.execute(update(Source).where(
        Source.id == source.id, Source.owner_id == owner_id, Source.deleted_at.is_(None))
        .values(deleted_at=utcnow()).execution_options(synchronize_session=False)).rowcount
    if not changed:
        raise ApiError(404, "not_found", "资料不存在")
    local = settings.environment == "local"
    job = DeletionJob(owner_id=owner_id, source_id=source.id, status="trashed" if local else "pending")
    db.add(job)
    if local:
        for model in (DocumentJob, OCRJob, WebReadJob):
            db.execute(update(model).where(model.source_id == source.id,
                       model.status.in_(("pending", "running")))
                       .values(status="cancelled", completed_at=utcnow(), last_error=None))
    if commit:
        db.commit()
        db.refresh(job)
    else:
        db.flush()
    return job
