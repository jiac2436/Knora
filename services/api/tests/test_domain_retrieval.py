"""Published hierarchy helps retrieval without becoming factual evidence."""
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app import domain_builds as builds, domain_retrieval, semantic, services
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing
from app.domain_summary_jobs import NodeEnqueue
from app.models import DomainSummaryArtifact, DomainTopic, DomainTopicMember, Source, SourceProject, SourceRevision, EvidenceChunk
from app.schemas import SearchRequest
from test_domain_builds import corpus, finish, add_documents
from test_domain_summary_jobs import prepared
from test_ai_analysis import storage
from test_summary_retrieval import analyzed


@pytest.fixture(autouse=True)
def no_local_model(monkeypatch):
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))


def build(data, scope='app', topic_id=None, text='lattice cohesion。'):
    db, _, source = data[:3]
    plan = builds.prepare_plan(db, source.owner_id, source.domain_id, scope, topic_id)
    authorize_processing(db, source.owner_id, source.domain_id, scope,
        DomainProcessingPolicy(enabled=True, max_calls=300, max_input_tokens=30000000), uuid4())
    queued = builds.enqueue_build(db, source.owner_id, source.domain_id,
        NodeEnqueue(request_id=str(uuid4()), scope=scope, topic_id=topic_id, preview_hash=builds.plan_hash(plan)))
    db.commit()
    def handle(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=dict(request_id=body['request_id'], provider='synthetic', model='test',
            prompt_version='test', usage=dict(prompt_tokens=10, completion_tokens=10),
            result=dict(overview=dict(text=text, citations=[entry['id'] for entry in body['evidence'][:8]]))))
    finish(data, [], httpx.MockTransport(handle))
    assert queued.status == 'completed'
    return queued


def search(data, query='lattice', *, agent=False, allowed=None, **options):
    return services.search_knowledge(data[0], data[2].owner_id, SearchRequest(query=query, **options),
        allowed_domains=allowed, for_agent=agent)


def test_node_keyword_returns_current_original_not_generated_prose(corpus):
    build(corpus)
    rows = search(corpus)
    assert len(rows) == 1
    row = rows[0]
    assert row['match_sources'] == ['knowledge_node']
    assert row['retrieval_confidence'] == 'candidate' and row['requires_evidence_confirmation']
    assert row['evidence_id'] == corpus[3].id and row['snippet'] == corpus[3].text
    assert 'lattice' not in row['snippet']
    node = row['knowledge_node']
    assert 'lattice' in node['snippet'] and node['role'] == 'retrieval_hint'
    assert node['kind'] == 'domain' and node['coverage']['original_text_complete'] is False


@pytest.mark.parametrize('change', ['title', 'deleted', 'private', 'membership'])
def test_stale_or_no_longer_visible_node_is_skipped(corpus, change):
    build(corpus, scope='shareable')
    assert search(corpus, agent=True, allowed={corpus[2].domain_id})
    if change == 'title': corpus[2].title = 'Changed'
    elif change == 'deleted': corpus[2].deleted_at = services.utcnow()
    elif change == 'private': corpus[2].visibility = 'app_only'
    else:
        corpus[0].add(Source(owner_id=corpus[2].owner_id, domain_id=corpus[2].domain_id,
            title='New member', kind='text', coverage='full_text'))
    corpus[0].commit()
    assert not search(corpus, agent=True, allowed={corpus[2].domain_id})


def test_app_summary_is_never_read_by_agent(corpus, monkeypatch):
    build(corpus)
    def forbidden(*args, **kwargs): pytest.fail('Private node response was loaded')
    monkeypatch.setattr(domain_retrieval, 'current_node_result', forbidden)
    assert not search(corpus, agent=True, allowed={corpus[2].domain_id})
    assert not search(corpus, agent=True, allowed=set())


def test_narrow_project_filter_rejects_mixed_node_before_read(corpus, monkeypatch):
    add_documents(corpus, 1)
    build(corpus, scope='shareable')
    db, _, source, _, _, project = corpus[:6]
    db.add(SourceProject(source_id=source.id, project_id=project.id)); db.commit()
    def forbidden(*args, **kwargs): pytest.fail('Mixed-scope node response was loaded')
    monkeypatch.setattr(domain_retrieval, 'current_node_result', forbidden)
    assert not search(corpus, agent=True, allowed={source.domain_id}, project_id=project.id)


def test_narrow_tag_filter_rejects_mixed_node_before_read(corpus, monkeypatch):
    from app import knowledge_tags
    add_documents(corpus, 1); build(corpus)
    tag = knowledge_tags.create_tag(knowledge_tags.CreateTag(name='Only one'), corpus[6], corpus[0])
    knowledge_tags.assign_tag(corpus[2].id, knowledge_tags.AssignmentRequest(tag_id=tag['id']), corpus[6], corpus[0])
    def forbidden(*args, **kwargs): pytest.fail('Mixed-scope node response was loaded')
    monkeypatch.setattr(domain_retrieval, 'current_node_result', forbidden)
    assert not search(corpus, tag_query='Only one')


def test_explicit_topic_node_can_be_used_in_narrow_project(corpus):
    add_documents(corpus, 1)
    db, _, source, _, _, project = corpus[:6]
    topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='shareable', name='Selected topic', name_fixed=True)
    db.add(topic); db.flush()
    db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True))
    db.add(SourceProject(source_id=source.id, project_id=project.id)); db.commit()
    analyzed(corpus)
    build(corpus, scope='shareable', topic_id=topic.id)
    rows = search(corpus, agent=True, allowed={source.domain_id}, project_id=project.id)
    assert len(rows) == 1 and rows[0]['source_id'] == source.id
    assert rows[0]['knowledge_node']['kind'] == 'topic'
    assert rows[0]['knowledge_node']['node_id'] == topic.id


def test_original_details_keep_the_same_order_and_limit(corpus):
    add_documents(corpus, 2)
    db, _, source = corpus[:3]
    before = search(corpus, query='原文', limit=1)
    build(corpus, text='原文 lattice cohesion。')
    after = search(corpus, query='原文', limit=1)
    assert [(row['source_id'], row['evidence_id'], row['score']) for row in after] == [
        (row['source_id'], row['evidence_id'], row['score']) for row in before]
    assert 'original' in after[0]['match_sources']


def test_newest_node_only_old_keyword_does_not_resurface(corpus):
    build(corpus, text='lattice cohesion。')
    build(corpus, text='nebula cohesion。')
    assert not search(corpus)
    assert search(corpus, query='nebula')


def test_missing_node_keeps_document_summary_and_raw_search(corpus):
    assert search(corpus, query='quasar')[0]['match_sources'] == ['summary']
    assert search(corpus, query=corpus[3].text)[0]['evidence_id'] == corpus[3].id


def test_rag_freezes_raw_text_only_and_retains_evidence_budget(corpus):
    from app.rag import TurnCreate, freeze_evidence
    build(corpus)
    evidence, metadata = freeze_evidence(corpus[0], corpus[2].owner_id,
        TurnCreate(session_id=uuid4(), question='lattice', confirm_send=True))
    assert len(evidence) == 1 and evidence[0].id == corpus[3].id
    assert evidence[0].text == corpus[3].text and 'lattice' not in evidence[0].text
    assert metadata[0]['knowledge_node']['role'] == 'retrieval_hint'
    assert 'snippet' not in metadata[0]['knowledge_node']
    assert len(evidence) <= 12 and sum(len(entry.text) for entry in evidence) <= 18000


def test_corrupt_latest_node_falls_back_without_serving_old_node(corpus):
    build(corpus)
    db, _, source = corpus[:3]
    root = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    db.add(DomainSummaryArtifact(id=str(uuid4()), owner_id=source.owner_id, domain_id=source.domain_id,
        scope='app', node_id=source.domain_id, snapshot_json=root.snapshot_json, response_json='not JSON'))
    db.commit()
    assert not search(corpus)
    assert search(corpus, query='quasar')[0]['match_sources'] == ['summary']


def test_declared_original_outside_scope_rejected_before_loading_response(corpus, monkeypatch):
    build(corpus, scope='shareable')
    db, _, source = corpus[:3]
    root = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    private = Source(owner_id=source.owner_id, domain_id=source.domain_id, title='Private', kind='text',
        visibility='app_only', coverage='full_text')
    db.add(private); db.flush()
    frozen = json.loads(root.snapshot_json)
    frozen['request']['evidence'][0]['source_id'] = private.id
    db.add(DomainSummaryArtifact(id=str(uuid4()), owner_id=source.owner_id, domain_id=source.domain_id,
        scope='shareable', node_id=source.domain_id, snapshot_json=json.dumps(frozen), response_json=root.response_json))
    db.commit()
    def forbidden(*args, **kwargs): pytest.fail('Out-of-scope original entered node processing')
    monkeypatch.setattr(domain_retrieval, 'current_node_result', forbidden)
    assert not search(corpus, agent=True, allowed={source.domain_id})


def test_semantic_fallback_and_node_only_candidates_can_coexist(corpus, monkeypatch):
    from types import SimpleNamespace
    add_documents(corpus, 1); build(corpus)
    count = []
    def hits(*args, **kwargs):
        count.append(True)
        if len(count) == 1: return []
        revision = corpus[0].get(SourceRevision, corpus[2].current_revision_id)
        return [(0.4, corpus[2], corpus[3], revision, SimpleNamespace(start=0))]
    monkeypatch.setattr(semantic, 'semantic_hits', hits)
    rows = search(corpus)
    assert len(rows) == 2 and len(count) == 2
    assert all(row['retrieval_confidence'] == 'candidate' for row in rows)


def test_mcp_endpoint_returns_node_hint_and_original_then_obeys_revoke(corpus):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.auth import AgentPrincipal, require_agent_principal
    from app.models import AgentConnection, ConnectionGrant, utcnow
    build(corpus, scope='shareable')
    db, _, source = corpus[:3]
    connection = AgentConnection(owner_id=source.owner_id, client_name='Synthetic agent', token_hash='a' * 64,
        expires_at=utcnow() + timedelta(days=1))
    db.add(connection); db.flush()
    grant = ConnectionGrant(connection_id=connection.id, domain_id=source.domain_id)
    db.add(grant); db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_agent_principal] = lambda: AgentPrincipal(source.owner_id, connection.id)
    try:
        with TestClient(app) as client:
            path = '/v1/mcp/tools/search_knowledge'
            response = client.post(path, json=dict(query='lattice'))
            assert response.status_code == 200, response.text
            row = response.json()['items'][0]
            assert row['evidence_id'] == corpus[3].id and row['snippet'] == corpus[3].text
            assert row['knowledge_node']['role'] == 'retrieval_hint'
            db.delete(grant); db.commit()
            assert client.post(path, json=dict(query='lattice')).json()['items'] == []
    finally:
        app.dependency_overrides.clear()


def test_v11_single_node_artifact_remains_searchable(corpus):
    from test_domain_summary_jobs import queue
    from services.worker import domain_summary
    job = queue(corpus)
    def handle(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=dict(request_id=body['request_id'], provider='synthetic', model='test',
            prompt_version='test', usage=dict(prompt_tokens=10, completion_tokens=10),
            result=dict(overview=dict(text='lattice cohesion。', citations=[body['evidence'][0]['id']]))))
    assert domain_summary.run_node_once(corpus[0], httpx.MockTransport(handle))
    assert job.status == 'completed'
    assert search(corpus)[0]['knowledge_node']['artifact_id'] == job.id


def test_search_never_creates_paid_jobs_or_embeds_nodes(corpus, monkeypatch):
    from app.models import DomainSummaryJob, DomainBuild
    build(corpus)
    before = corpus[0].scalars(select(DomainSummaryJob.id)).all()
    plans = corpus[0].scalars(select(DomainBuild.id)).all()
    monkeypatch.setattr(semantic, 'embed_documents', lambda *_: pytest.fail('Node embedded during query'))
    assert search(corpus)
    assert corpus[0].scalars(select(DomainSummaryJob.id)).all() == before
    assert corpus[0].scalars(select(DomainBuild.id)).all() == plans


def test_new_wider_node_between_metadata_check_and_lock_is_never_read(corpus, monkeypatch):
    build(corpus, scope='shareable')
    db, _, source, _, _, project = corpus[:6]
    db.add(SourceProject(source_id=source.id, project_id=project.id)); db.commit()
    root = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    original_lock = domain_retrieval.locked_state
    def publish_wider_before_lock(*args, **kwargs):
        other = Source(owner_id=source.owner_id, domain_id=source.domain_id, title='Other project',
            kind='text', coverage='full_text')
        db.add(other); db.flush()
        frozen = json.loads(root.snapshot_json)
        dependency = dict(source_id=other.id, revision_id=None, summary_artifact_id=None, input_sha256='0' * 64)
        frozen['global_manifest']['sources'].append(dependency)
        db.add(DomainSummaryArtifact(id=str(uuid4()), owner_id=source.owner_id, domain_id=source.domain_id,
            scope='shareable', node_id=source.domain_id, snapshot_json=json.dumps(frozen), response_json=root.response_json))
        db.flush()
        return original_lock(*args, **kwargs)
    monkeypatch.setattr(domain_retrieval, 'locked_state', publish_wider_before_lock)
    def forbidden(*args, **kwargs): pytest.fail('New wider response was deserialized')
    monkeypatch.setattr(domain_retrieval.DomainSummaryResponse, 'model_validate_json', forbidden)
    assert not search(corpus, agent=True, allowed={source.domain_id}, project_id=project.id)
