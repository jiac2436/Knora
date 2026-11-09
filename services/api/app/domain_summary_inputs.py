"""Freeze one bounded node request from valid summaries and their original text."""
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain_contract import (DomainContextSummary, DomainEvidence, DomainSummaryRequest,
                              DomainSummaryResponse, validate_domain_summary)
from .domain_inputs import freeze_domain_inputs, manifest_fingerprint
from .domain_processing import DomainInputManifest, DomainScope
from .domain_runtime import locked_state
from .domain_topics import require_owned_domain, require_topic, source_scope
from .errors import ApiError
from .config import settings
from .models import AIArtifact, DomainTopicMember, EvidenceChunk, Source


# Keep citations precise enough to combine several documents within the
# bounded parent synthesis budget. Stored requests are intact.
DOMAIN_CITATION_WINDOW = 1000


def document_input(db: Session, dependency):
    """Read one valid summary and its exact originals once per planning pass."""
    artifact = db.get(AIArtifact, dependency.summary_artifact_id) if dependency.summary_artifact_id else None
    if artifact is None:
        return None, {}, "no_valid_summary"
    result, snapshot = json.loads(artifact.result_json), json.loads(artifact.snapshot_json)
    text = result["summary"]
    if len(text) > 800:
        return None, {}, "summary_too_long"
    entries = {e["id"]: e for e in snapshot["evidence"]}
    selected = {}
    for identifier in result["summary_citations"]:
        entry = entries[identifier]
        original_id = entry.get("original_evidence_id") or identifier
        original = db.get(EvidenceChunk, original_id)
        start = entry.get("text_start", 0)
        part = entry["text"]
        end = start + len(part)
        if original is None or original.text[start:end] != part:
            return None, {}, "evidence_not_bounded"
        # Preserve every substantive part of a valid citation. A long original
        # is input data, not a reason to exclude an already analyzed document.
        # Stable window IDs allow downstream citations to resolve exact offsets.
        for offset in range(0, len(part), DOMAIN_CITATION_WINDOW):
            window_text = part[offset:offset + DOMAIN_CITATION_WINDOW]
            if not window_text.strip():
                continue
            window_start, window_end = start + offset, start + offset + len(window_text)
            window_id = identifier if len(part) <= DOMAIN_CITATION_WINDOW else str(uuid5(NAMESPACE_URL,
                f'domain-evidence:{original_id}:{window_start}:{window_end}'))
            selected[window_id] = DomainEvidence(id=window_id, original_evidence_id=original_id,
                source_id=dependency.source_id, revision_id=dependency.revision_id,
                text=window_text, text_start=window_start, text_end=window_end,
                locator_kind=original.locator_kind, locator_start=original.locator_start, locator_end=original.locator_end)
    if not selected:
        return None, {}, "evidence_not_bounded"
    return DomainContextSummary(node_id=artifact.id, kind="document", text=text,
        source_ids=[dependency.source_id], citations=list(selected)), selected, None


def build_node_request(db: Session, owner: str, domain_id: str, scope: DomainScope,
                       request_id: UUID, topic_id: str | None = None,
                       allowed_source_ids: frozenset[str] | None = None, validation_context=None) -> dict:
    domain = validation_context.domain(owner, domain_id, scope) if validation_context else require_owned_domain(db, owner, domain_id)
    query = source_scope(owner, domain_id, scope)
    if topic_id:
        topic = validation_context.topic(owner, domain_id, scope, topic_id) if validation_context else require_topic(db, owner, domain_id, topic_id)
        if topic.scope != scope:
            raise ApiError(404, "not_found", "主题不存在")
        query = query.where(Source.id.in_(select(DomainTopicMember.source_id).where(
            DomainTopicMember.topic_id == topic_id, DomainTopicMember.included.is_(True))))
        title = topic.name
    else:
        title = domain.name
    if allowed_source_ids is not None:
        query = query.where(Source.id.in_(allowed_source_ids))
    if validation_context:
        ids = validation_context.node_source_ids(owner, domain_id, scope, topic_id)
        if allowed_source_ids is not None:
            ids = ids & allowed_source_ids
        manifest = validation_context.manifest(owner, domain_id, scope, allowed_source_ids=ids)
    else:
        ids = frozenset(db.scalars(query.with_only_columns(Source.id)))
        manifest = freeze_domain_inputs(db, owner, domain_id, scope=scope, allowed_source_ids=ids)
    summaries, evidence, excluded = [], {}, {}
    text_count = context_count = 0
    for dependency in manifest.sources:
        context, selected, reason = document_input(db, dependency)
        if reason:
            excluded[dependency.source_id] = reason
            continue
        text = context.text
        new_count = sum(len(e.text) for key, e in selected.items() if key not in evidence)
        if (len(summaries) == 20 or len(evidence.keys() | selected.keys()) > 80 or
                text_count + new_count > 18000 or context_count + len(text) > 6000):
            excluded[dependency.source_id] = "batch_budget"
            continue
        summaries.append(context)
        evidence.update(selected)
        text_count += new_count; context_count += len(text)
    if not summaries:
        raise ApiError(409, "no_eligible_summary", "没有可纳入的有效单篇摘要及有界原文证据；不会自动补做单篇分析")
    request = DomainSummaryRequest(request_id=str(request_id), node_id=topic_id or domain_id,
        kind="topic" if topic_id else "domain", title=title, summaries=summaries, evidence=list(evidence.values()))
    from .model_configuration import frozen_model_binding
    return dict(**frozen_model_binding(settings), request=request.model_dump(), manifest=manifest.model_dump(),
        coverage=dict(source_count=len(ids), included_source_count=len(summaries), excluded=excluded,
            all_sources_selected=len(summaries) == len(ids), original_text_complete=False,
            basis="valid_document_summaries_and_cited_original_excerpts"))


def validate_node_publication(db: Session, owner: str, domain_id: str, scope: DomainScope,
                              frozen: dict, response: DomainSummaryResponse, validation_context=None) -> None:
    """Publication precondition under a short SQLite write transaction.

    The publisher must insert the artifact and commit in this same
    transaction. This function does not publish or commit and holds no HTTP call.
    """
    if db.get_bind().dialect.name != 'sqlite':
        raise ValueError("Node publication currently requires local SQLite")
    request = DomainSummaryRequest.model_validate(frozen['request'])
    manifest = DomainInputManifest.model_validate(frozen['manifest'])
    if (manifest.owner_id, manifest.domain_id, manifest.scope) != (owner, domain_id, scope):
        raise ApiError(409, 'snapshot_binding_mismatch', '概览输入范围与任务不一致')
    if response.request_id != request.request_id or (request.kind == 'domain' and request.node_id != domain_id):
        raise ApiError(409, 'snapshot_binding_mismatch', '概览请求编号或节点不一致')
    validate_domain_summary(request, response.result)
    if validation_context:
        validation_context.check()
    else:
        locked_state(db, owner, domain_id, scope)
    try:
        current = build_node_request(db, owner, domain_id, scope, UUID(request.request_id),
                                     request.node_id if request.kind == 'topic' else None, validation_context=validation_context)
    except ApiError as error:
        raise ApiError(409, 'stale_node_inputs', '资料、授权范围或有效摘要已变化，概览不应发布') from error
    current_manifest = DomainInputManifest.model_validate(current['manifest'])
    if (manifest_fingerprint(manifest) != manifest_fingerprint(current_manifest)
            or request != DomainSummaryRequest.model_validate(current['request']) or frozen['coverage'] != current['coverage']):
        raise ApiError(409, 'stale_node_inputs', '资料、主题或覆盖范围已变化，概览不应发布')
