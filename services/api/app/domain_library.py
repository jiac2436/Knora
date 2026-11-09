"""Owner-only, read-only library pages. No index creation or model work."""
from collections import defaultdict
from datetime import datetime, timezone
import json
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import semantic
from .ai import context_view, is_stale
from .ai_contract import Identifier, StrictModel
from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .domain_contract import DomainSummaryRequest
from .domain_node_index import node_index_status
from .domain_query_snapshot import NodeQuerySnapshot, input_batches
from .domain_retrieval import eligible_nodes, resolve_original
from .domain_summary_jobs import current_node_result
from .domain_topics import require_owned_domain, require_topic, source_scope
from .errors import ApiError
from .models import (AIArtifact, AIJob, DomainNodeEmbedding, DomainSummaryArtifact, DomainTopic,
    DomainTopicMember, EvidenceChunk, EvidenceEmbedding, FileAsset, KnowledgeTag, PersonalContext,
    Project, SemanticIndexJob, Source, SourceImage, SourceProject, SourceRevision, SourceSearchDocument, SourceSearchTerm, SummaryEmbedding,
    SummaryIndexJob, SummarySearchTerm, TagAssignment)
from .summary_index import _terms, current_summary_artifact, summary_text
from .summary_retrieval import summary_evidence, valid_assignments

router = APIRouter(prefix='/v1/domains', tags=['domain-library'])


class AppScope(StrictModel):
    scope: Literal['app'] = 'app'


class LibraryRequest(AppScope):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class LibrarySourcesRequest(LibraryRequest):
    limit: int = Field(default=40, ge=1, le=100)
    topic_id: str | None = Field(default=None, min_length=1, max_length=36)
    unclassified: bool = False

    @model_validator(mode='after')
    def one_branch(self):
        if self.topic_id and self.unclassified:
            raise ValueError('Select a topic or unclassified sources')
        return self


class InformationRequest(AppScope):
    tag_offset: int = Field(default=0, ge=0)
    tag_limit: int = Field(default=100, ge=1, le=100)


class CitationsRequest(AppScope):
    citation_ids: list[Identifier] = Field(min_length=1, max_length=80)


def directory_rows(db, owner, domain):
    require_owned_domain(db, owner, domain)
    available = set(db.scalars(source_scope(owner, domain, 'app').with_only_columns(Source.id)))
    topics = list(db.scalars(select(DomainTopic).where(DomainTopic.owner_id == owner,
        DomainTopic.domain_id == domain, DomainTopic.scope == 'app').order_by(DomainTopic.name, DomainTopic.id)))
    members = defaultdict(set)
    for batch in input_batches([topic.id for topic in topics]):
        for topic_id, source_id in db.execute(select(DomainTopicMember.topic_id, DomainTopicMember.source_id)
                .where(DomainTopicMember.topic_id.in_(batch), DomainTopicMember.included.is_(True))):
            if source_id in available:
                members[topic_id].add(source_id)
    # Match the existing topic-directory visibility rule: empty automatic
    # candidates disappear; manual folders and populated ancestors remain.
    active_ids = {t.id for t in topics if members[t.id] or t.automatic_key is None or t.name_fixed}
    active_ids.update(t.parent_id for t in topics if t.id in active_ids and t.parent_id)
    topics = [t for t in topics if t.id in active_ids]
    return available, topics, members


def library_view(db, owner, domain, body):
    available, topics, members = directory_rows(db, owner, domain)
    subtree = {t.id: set(members[t.id]) for t in topics}
    for topic in topics:
        if topic.parent_id in subtree:
            subtree[topic.parent_id].update(members[topic.id])
    assigned = set().union(*members.values()) if members else set()
    page = topics[body.offset:body.offset + body.limit]
    return dict(domain_id=domain, scope='app', source_count=len(available), topic_count=len(topics),
        unclassified_count=len(available - assigned),
        items=[dict(id=t.id, name=t.name, parent_id=t.parent_id, source_count=len(subtree[t.id]),
                    direct_source_count=len(members[t.id])) for t in page],
        next_offset=body.offset + len(page) if body.offset + len(page) < len(topics) else None)


def library_sources_view(db, owner, domain, body):
    require_owned_domain(db, owner, domain)
    query = source_scope(owner, domain, 'app')
    if body.topic_id:
        topic = require_topic(db, owner, domain, body.topic_id)
        if topic.scope != 'app':
            raise ApiError(404, 'not_found', '分类不存在')
        query = query.where(Source.id.in_(select(DomainTopicMember.source_id).where(
            DomainTopicMember.topic_id == topic.id, DomainTopicMember.included.is_(True))))
    elif body.unclassified:
        assigned = select(DomainTopicMember.source_id).join(DomainTopic,
            DomainTopicMember.topic_id == DomainTopic.id).where(DomainTopic.owner_id == owner,
            DomainTopic.domain_id == domain, DomainTopic.scope == 'app', DomainTopicMember.included.is_(True))
        query = query.where(Source.id.not_in(assigned))
    count = db.scalar(select(func.count()).select_from(query.with_only_columns(Source.id).subquery()))
    rows = db.execute(query.with_only_columns(Source.id, Source.title, Source.current_revision_id)
        .order_by(Source.title, Source.id).offset(body.offset).limit(body.limit)).all()
    return dict(domain_id=domain, scope='app', topic_id=body.topic_id, unclassified=body.unclassified,
        total_count=count, items=[dict(id=r.id, title=r.title, revision_id=r.current_revision_id) for r in rows],
        next_offset=body.offset + len(rows) if body.offset + len(rows) < count else None)


def current_documents(db, owner, domain, sources):
    """Batch candidates and context; retain current evidence objects for db.get."""
    ids = [s.id for s in sources]
    candidates, contexts, projects = defaultdict(list), {}, defaultdict(list)
    historical, jobs, revisions, evidence = set(), defaultdict(list), [], []
    for batch in input_batches(ids):
        for artifact in db.scalars(select(AIArtifact).where(AIArtifact.owner_id == owner,
                AIArtifact.source_id.in_(batch)).order_by(AIArtifact.created_at.desc(), AIArtifact.id.desc())):
            historical.add(artifact.source_id)
            candidates[artifact.source_id].append(artifact)
        contexts.update((r.source_id, r) for r in db.scalars(select(PersonalContext).where(PersonalContext.source_id.in_(batch))))
        for source_id, project in db.execute(select(SourceProject.source_id, Project).join(Project,
                Project.id == SourceProject.project_id).where(SourceProject.source_id.in_(batch)).order_by(Project.id)):
            projects[source_id].append(project)
        for job in db.scalars(select(AIJob).where(AIJob.owner_id == owner, AIJob.source_id.in_(batch),
                AIJob.kind == 'analyze').order_by(AIJob.created_at.desc(), AIJob.id.desc())):
            jobs[job.source_id].append(job)
    for batch in input_batches([s.current_revision_id for s in sources if s.current_revision_id]):
        revisions.extend(db.scalars(select(SourceRevision).where(SourceRevision.id.in_(batch))))
        evidence.extend(db.scalars(select(EvidenceChunk).where(EvidenceChunk.revision_id.in_(batch))))
    current, counts = {}, dict(total=len(sources), analyzed=0, pending=0, outdated=0, running=0, failed=0)
    domain_row = require_owned_domain(db, owner, domain)
    for source in sources:
        current_context = context_view(source, contexts.get(source.id), domain_row, projects[source.id])
        artifact = current_summary_artifact(db, source, candidates=candidates[source.id],
            current_context=current_context)
        current_job = None
        for job in jobs[source.id]:
            try:
                if not is_stale(db, source, json.loads(job.snapshot_json), current_context=current_context):
                    current_job = job
                    break
            except (ValueError, TypeError, KeyError):
                continue
        if artifact is not None and summary_evidence(db, source, artifact) is None:
            artifact = None
        if artifact is not None:
            current[source.id] = artifact
            status = 'analyzed'
        elif current_job and current_job.status in ('pending', 'running'):
            status = 'running'
        elif current_job and current_job.status in ('failed', 'unknown'):
            status = 'failed'
        else:
            status = 'outdated' if source.id in historical else 'pending'
        counts[status] += 1
    return current, counts, revisions, evidence


def current_nodes(db, owner, domain):
    """Validate the scoped node set once, sharing the expensive input manifest."""
    scope = source_scope(owner, domain, 'app')
    rows = list(db.scalars(eligible_nodes(owner, scope, False).where(DomainSummaryArtifact.domain_id == domain)))
    valid = {}
    if not rows:
        return valid
    try:
        context = NodeQuerySnapshot(db, owner, scope, rows)
    except (ApiError, ValueError, TypeError, KeyError):
        return valid
    for artifact in rows:
        try:
            frozen = json.loads(artifact.snapshot_json)
            topic_id = frozen.get('published_topic_id') or (artifact.node_id if artifact.node_id != domain else None)
            result = current_node_result(db, owner, domain, 'app', topic_id,
                validation_context=context, artifact=artifact)
            if result['available']:
                valid[topic_id or domain] = (artifact, result)
        except (ApiError, ValueError, TypeError, KeyError):
            continue
    return valid


def tag_information(db, owner, domain, body):
    catalog = {}
    scope = source_scope(owner, domain, 'app').with_only_columns(Source.id)
    for tag, origin in db.execute(select(KnowledgeTag, TagAssignment.origin).join(TagAssignment,
            TagAssignment.tag_id == KnowledgeTag.id).join(Source, Source.id == TagAssignment.source_id)
            .where(KnowledgeTag.owner_id == owner, Source.id.in_(scope), valid_assignments())
            .order_by(KnowledgeTag.name, KnowledgeTag.id, TagAssignment.origin)):
        catalog.setdefault(tag.id, dict(id=tag.id, name=tag.name, origins=set()))['origins'].add(origin)
    values = [dict(item, origins=sorted(item['origins'])) for item in catalog.values()]
    page = values[body.tag_offset:body.tag_offset + body.tag_limit]
    return dict(total=len(values), manual=sum('manual' in v['origins'] for v in values),
        ai=sum('ai' in v['origins'] for v in values), both=sum(len(v['origins']) == 2 for v in values), items=page,
        next_offset=body.tag_offset + len(page) if body.tag_offset + len(page) < len(values) else None)


def layer(total):
    return dict(total=total, ready=0, pending=0, failed=0, disabled=0, unavailable=0, chunk_count=0)


def document_indexes(db, owner, sources, current, evidence):
    original, summary = layer(len(sources)), layer(len(sources))
    jobs, summary_jobs, terms, raw_vectors, summary_vectors = {}, {}, defaultdict(set), defaultdict(list), defaultdict(list)
    by_revision = defaultdict(list)
    for entry in evidence:
        by_revision[entry.revision_id].append(entry)
    for batch in input_batches([s.id for s in sources]):
        jobs.update((j.source_id, j) for j in db.scalars(select(SemanticIndexJob).where(SemanticIndexJob.source_id.in_(batch))))
        summary_jobs.update((j.source_id, j) for j in db.scalars(select(SummaryIndexJob).where(SummaryIndexJob.source_id.in_(batch))))
        for row in db.execute(select(EvidenceEmbedding.source_id, EvidenceEmbedding.revision_id,
                EvidenceEmbedding.evidence_id, EvidenceEmbedding.start, EvidenceEmbedding.end, EvidenceEmbedding.vector)
                .where(EvidenceEmbedding.owner_id == owner, EvidenceEmbedding.source_id.in_(batch), EvidenceEmbedding.model == semantic.MODEL_NAME)):
            raw_vectors[row.source_id].append(row)
        for row in db.execute(select(SummaryEmbedding.source_id, SummaryEmbedding.artifact_id, SummaryEmbedding.revision_id,
                SummaryEmbedding.start, SummaryEmbedding.end, SummaryEmbedding.vector).where(SummaryEmbedding.owner_id == owner,
                SummaryEmbedding.source_id.in_(batch), SummaryEmbedding.model == semantic.MODEL_NAME)):
            summary_vectors[row.source_id].append(row)
        for artifact_id, term in db.execute(select(SummarySearchTerm.artifact_id, SummarySearchTerm.term)
                .where(SummarySearchTerm.owner_id == owner, SummarySearchTerm.source_id.in_(batch))):
            terms[artifact_id].add(term)
    vector_bytes = 0
    for source in sources:
        raw = [v for v in raw_vectors[source.id] if v.revision_id == source.current_revision_id]
        vector_bytes += sum(len(v.vector) for v in raw)
        original['chunk_count'] += len(raw)
        job = jobs.get(source.id)
        expected = sorted((e.id, start, end) for e in by_revision[source.current_revision_id]
                          for start, end, _ in semantic.windows(e.text))
        actual = sorted((v.evidence_id, v.start, v.end) for v in raw)
        if not semantic.settings.semantic_enabled:
            state = 'disabled'
        elif not source.current_revision_id or not expected:
            state = 'unavailable'
        elif job and job.revision_id == source.current_revision_id and job.status == 'failed':
            state = 'failed'
        elif job and job.revision_id == source.current_revision_id and job.status == 'completed' and actual == expected:
            state = 'ready'
        else:
            state = 'pending'
        original[state] += 1
        artifact = current.get(source.id)
        if artifact is None:
            summary['unavailable'] += 1
            continue
        vectors = [v for v in summary_vectors[source.id] if v.artifact_id == artifact.id and v.revision_id == source.current_revision_id]
        vector_bytes += sum(len(v.vector) for v in vectors)
        summary['chunk_count'] += len(vectors)
        job = summary_jobs.get(source.id)
        text = summary_text(artifact)
        complete = terms[artifact.id] == _terms(text)
        if semantic.settings.semantic_enabled:
            complete = complete and sorted((v.start, v.end) for v in vectors) == [(a, b) for a, b, _ in semantic.windows(text)]
        state = ('disabled' if not semantic.settings.semantic_enabled else
                 'failed' if job and job.artifact_id == artifact.id and job.status == 'failed' else
                 'ready' if job and job.artifact_id == artifact.id and job.status == 'completed' and complete else 'pending')
        summary[state] += 1
    return original, summary, vector_bytes


def information_view(db, owner, domain, body):
    available, topics, _ = directory_rows(db, owner, domain)
    sources = list(db.scalars(source_scope(owner, domain, 'app').order_by(Source.id)))
    current, documents, revisions, evidence = current_documents(db, owner, domain, sources)
    nodes = current_nodes(db, owner, domain)
    original, summary, vector_bytes = document_indexes(db, owner, sources, current, evidence)
    layers = dict(original=original, summary=summary, topics=layer(len(topics)), domain=layer(1))
    for identifier in [domain, *[t.id for t in topics]]:
        item = nodes.get(identifier)
        target = layers['domain' if identifier == domain else 'topics']
        if item is None:
            target['unavailable'] += 1
            continue
        artifact, _ = item
        status = node_index_status(db, artifact.id)
        target[status if status in ('ready', 'failed', 'disabled') else 'pending'] += 1
        vectors = list(db.scalars(select(DomainNodeEmbedding).where(DomainNodeEmbedding.artifact_id == artifact.id,
            DomainNodeEmbedding.model == semantic.MODEL_NAME)))
        target['chunk_count'] += len(vectors)
        vector_bytes += sum(len(v.vector) for v in vectors)
    recorded_assets = 0
    for batch in input_batches(sorted(available)):
        recorded_assets += sum(db.scalars(select(FileAsset.size_bytes).where(FileAsset.source_id.in_(batch))))
        recorded_assets += sum(value or 0 for value in db.scalars(select(SourceImage.size_bytes).join(Source,
            Source.id == SourceImage.source_id).where(Source.id.in_(batch), SourceImage.revision_id == Source.current_revision_id,
            SourceImage.status == 'saved')))
    text_bytes = sum(len(r.extracted_text.encode('utf-8')) for r in revisions)
    artifacts = list(current.values()) + [item[0] for item in nodes.values()]
    ai_bytes = sum(len((a.result_json if isinstance(a, AIArtifact) else a.response_json).encode('utf-8'))
        + len(a.snapshot_json.encode('utf-8')) for a in artifacts)
    storage = dict(total_bytes=None, physical_bytes=None, recorded_asset_bytes=recorded_assets,
        text_utf8_bytes=text_bytes, ai_payload_bytes=ai_bytes, vector_payload_bytes=vector_bytes,
        known_payload_bytes=recorded_assets + text_bytes + ai_bytes + vector_bytes, measurement='logical_partial',
        note='记录的附件大小及当前逻辑数据量；不含 SQLite 页、倒排索引、历史版本和共享文件去重等物理开销')
    indexed = db.scalar(select(SourceSearchDocument.id).join(SourceSearchTerm,
        SourceSearchTerm.document_id == SourceSearchDocument.id).where(SourceSearchDocument.owner_id == owner,
        SourceSearchDocument.source_id.in_(source_scope(owner, domain, 'app').with_only_columns(Source.id)),
        SourceSearchTerm.term == '__indexed__').limit(1)) is not None
    return dict(domain_id=domain, scope='app', computed_at=datetime.now(timezone.utc).isoformat(), documents=documents,
        topics=dict(total=len(topics), roots=sum(t.parent_id is None for t in topics), children=sum(t.parent_id is not None for t in topics)),
        tags=tag_information(db, owner, domain, body), storage=storage,
        rag=dict(semantic_enabled=semantic.settings.semantic_enabled, keyword_available=indexed, layers=layers),
        overview_coverage=nodes[domain][1]['coverage'] if domain in nodes else None)


def citations_view(db, owner, domain, identifier, body):
    require_owned_domain(db, owner, domain)
    scope = source_scope(owner, domain, 'app')
    # Gate the latest public domain/topic artifact before reading generated text.
    artifact = db.scalar(eligible_nodes(owner, scope, False).where(DomainSummaryArtifact.id == identifier,
        DomainSummaryArtifact.domain_id == domain))
    if artifact is None:
        raise ApiError(404, 'not_found', '当前概览依据不存在')
    try:
        context = NodeQuerySnapshot(db, owner, scope, [artifact])
        frozen = json.loads(artifact.snapshot_json)
        topic_id = frozen.get('published_topic_id') or (artifact.node_id if artifact.node_id != domain else None)
        current = current_node_result(db, owner, domain, 'app', topic_id, validation_context=context, artifact=artifact)
        if not current['available']:
            raise ValueError('Stale result')
        request = DomainSummaryRequest.model_validate(frozen['request'])
        cited = {c for p in [current['result']['overview'], *current['result']['insights'], *current['result']['limitations']]
                 for c in p['citations']}
        entries = {e.id: e for e in request.evidence}
        if any(c not in cited for c in body.citation_ids):
            raise ApiError(404, 'not_found', '原文依据不存在')
        items = []
        for citation in dict.fromkeys(body.citation_ids):
            entry = entries[citation]
            resolved = resolve_original(db, owner, entry, scope, validation_context=context)
            if resolved is None:
                raise ValueError('Stale original')
            source, evidence, revision, start = resolved
            locator_start = evidence.locator_start + start if evidence.locator_kind == 'text' else evidence.locator_start
            locator_end = locator_start + len(entry.text) if evidence.locator_kind == 'text' else evidence.locator_end
            items.append(dict(id=citation, source_id=source.id, title=source.title, revision_id=revision.id,
                evidence_id=evidence.id, text=entry.text, locator_kind=evidence.locator_kind,
                locator_start=locator_start, locator_end=locator_end,
                text_start=start, text_end=start + len(entry.text)))
        context.check()
        return dict(domain_id=domain, scope='app', artifact_id=identifier, items=items)
    except ApiError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ApiError(409, 'stale_inputs', '概览或原文已变化，请刷新概览')


@router.post('/{domain_id}/analysis/library')
def read_library(domain_id: str, body: LibraryRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return library_view(db, principal.owner_id, domain_id, body)


@router.post('/{domain_id}/analysis/library/sources')
def read_library_sources(domain_id: str, body: LibrarySourcesRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return library_sources_view(db, principal.owner_id, domain_id, body)


@router.post('/{domain_id}/analysis/information')
def read_information(domain_id: str, body: InformationRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return information_view(db, principal.owner_id, domain_id, body)


@router.post('/{domain_id}/analysis/citations/{artifact_id}')
def read_citations(domain_id: str, artifact_id: str, body: CitationsRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return citations_view(db, principal.owner_id, domain_id, artifact_id, body)
