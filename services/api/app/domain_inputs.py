"""Read-only, permission-scoped input manifests for domain processing."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain_processing import DomainInputManifest, DomainScope, DomainSourceDependency
from .errors import ApiError
from .models import DomainTrash
from .models import (Domain, EvidenceChunk, ExperienceRecord, PersonalContext,
                     Source, SourceProject, SourceRevision, DomainTopic, DomainTopicMember)
from .summary_index import current_summary_artifact
from .summary_retrieval import source_tags, summary_evidence


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def manifest_fingerprint(manifest: DomainInputManifest) -> str:
    payload = manifest.model_dump(mode="json")
    payload["sources"] = sorted(payload["sources"], key=lambda item: item["source_id"])
    return _digest(payload)


def freeze_domain_inputs(db: Session, owner_id: str, domain_id: str, *,
                         scope: DomainScope = "app",
                         allowed_source_ids: frozenset[str] | None = None, prefetched=None) -> DomainInputManifest:
    """Freeze dependencies, not a model prompt. Caller provides Agent grants.

    Project/tag restrictions must be resolved before calling this function.
    No raw inputs, credentials, or private catalog are returned in the manifest.
    """
    if scope not in {"app", "shareable"}:
        raise ValueError("Unsupported domain scope")
    domain = db.scalar(select(Domain).where(Domain.id == domain_id, Domain.owner_id == owner_id))
    if domain is None or db.get(DomainTrash, domain_id) is not None:
        raise ApiError(404, "not_found", "知识域不存在")
    query = select(Source).where(Source.owner_id == owner_id, Source.domain_id == domain_id,
        Source.deleted_at.is_(None), Source.processing_status.not_in(("deleting", "deleted")))
    if scope == "shareable":
        query = query.where(Source.visibility == "app_and_agents")
    if allowed_source_ids is not None:
        query = query.where(Source.id.in_(allowed_source_ids))
    dependencies = []
    for source in db.scalars(query.order_by(Source.id)):
        if prefetched is None:
            revision = db.scalar(select(SourceRevision).where(
                SourceRevision.id == source.current_revision_id, SourceRevision.source_id == source.id))
            evidence = list(db.execute(select(EvidenceChunk.id, EvidenceChunk.ordinal,
                EvidenceChunk.text, EvidenceChunk.locator_kind, EvidenceChunk.locator_start,
                EvidenceChunk.locator_end).where(EvidenceChunk.revision_id == revision.id)
                .order_by(EvidenceChunk.ordinal, EvidenceChunk.id))) if revision else []
            context = db.get(PersonalContext, source.id)
            experiences = list(db.execute(select(ExperienceRecord.id, ExperienceRecord.project_id,
                ExperienceRecord.result, ExperienceRecord.notes, ExperienceRecord.verified_at).where(
                ExperienceRecord.owner_id == owner_id, ExperienceRecord.source_id == source.id)
                .order_by(ExperienceRecord.id)))
            artifact = current_summary_artifact(db, source)
            tags = source_tags(db, source)
            projects = sorted(db.scalars(select(SourceProject.project_id).where(SourceProject.source_id == source.id)))
        else:
            prefetched.check_source(db, source)
            revision = prefetched.revision(source)
            evidence = prefetched.evidence_rows.get(revision.id, []) if revision else []
            context = prefetched.contexts.get(source.id)
            experiences = prefetched.experiences.get(source.id, [])
            artifact = current_summary_artifact(db, source, candidates=prefetched.candidates.get(source.id, []),
                                               current_context=prefetched.context_state(source))
            tags = [dict(item, origins=list(item['origins'])) for item in prefetched.tags.get(source.id, [])]
            projects = prefetched.project_ids.get(source.id, [])
        if artifact is not None and summary_evidence(db, source, artifact) is None:
            artifact = None
        tags = sorted(tags, key=lambda item: item["id"])
        for tag in tags:
            tag["origins"] = sorted(tag["origins"])
        payload = dict(title=source.title, kind=source.kind, coverage=source.coverage,
            visibility=source.visibility, domain_id=source.domain_id,
            raw_input=source.raw_input, raw_url=source.raw_url, canonical_url=source.canonical_url,
            revision_id=source.current_revision_id, text=revision.extracted_text if revision else None,
            evidence=[list(row) for row in evidence], tags=tags, project_ids=projects,
            context=[context.reason, context.notes, context.applicability, context.version] if context else None,
            experiences=[[*row[:4], row.verified_at.isoformat()] for row in experiences],
            summary=[artifact.id, artifact.result_json, artifact.snapshot_json] if artifact else None)
        dependencies.append(DomainSourceDependency(source_id=source.id,
            revision_id=source.current_revision_id,
            summary_artifact_id=artifact.id if artifact else None, input_sha256=_digest(payload)))
    topics = db.scalars(select(DomainTopic).where(DomainTopic.owner_id == owner_id,
        DomainTopic.domain_id == domain_id, DomainTopic.scope == scope).order_by(DomainTopic.id)).all()
    available = {item.source_id for item in dependencies}
    manual_members = list(db.execute(select(DomainTopicMember.topic_id, DomainTopicMember.source_id,
        DomainTopicMember.included).where(DomainTopicMember.topic_id.in_([t.id for t in topics]),
        DomainTopicMember.source_id.in_(available), DomainTopicMember.manual.is_(True))
        .order_by(DomainTopicMember.topic_id, DomainTopicMember.source_id)))
    manual_topic_ids = {row.topic_id for row in manual_members}
    structure = dict(topics=[[t.id, t.name, t.parent_id, t.name_fixed, t.automatic_key] for t in topics
        if t.name_fixed or t.parent_id or t.automatic_key is None or t.id in manual_topic_ids],
        members=[list(row) for row in manual_members])
    return DomainInputManifest(owner_id=owner_id, domain_id=domain_id, domain_name=domain.name,
                               scope=scope, sources=dependencies, structure_sha256=_digest(structure))


def dependencies_are_current(published: DomainInputManifest, current: DomainInputManifest) -> bool:
    """Fail closed before reading generated content, including filtered scopes."""
    if (published.owner_id, published.domain_id, published.scope, published.rules_version) != (
            current.owner_id, current.domain_id, current.scope, current.rules_version):
        return False
    if published.domain_name != current.domain_name:
        return False
    if published.structure_sha256 != current.structure_sha256:
        return False
    allowed = {item.source_id: item for item in current.sources}
    # Empty manifests support directory metadata only, never generated facts.
    return (bool(published.sources) and len(published.sources) == len(current.sources)
            and all(allowed.get(item.source_id) == item for item in published.sources))
