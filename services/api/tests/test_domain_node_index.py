"""Local-only node vector jobs, scoped semantic retrieval and navigation."""
from dataclasses import replace
import json
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import delete, select

from app import domain_node_index as index, domain_navigation as navigation, semantic, services
from app.domain_navigation import NavigationRequest
from app.domain_runtime import locked_state
from app.models import (DomainNodeEmbedding, DomainNodeIndexJob, DomainSummaryArtifact, DomainProcessingState,
    DomainTopic, DomainTopicMember, DomainSummaryJob, SourceProject)
from app.schemas import SearchRequest
from test_domain_retrieval import build, corpus, prepared, storage, no_local_model, search
from test_domain_builds import add_documents
from test_summary_retrieval import analyzed


@pytest.fixture
def indexed(corpus, monkeypatch):
    build(corpus)
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True, semantic_min_similarity=0.5))
    monkeypatch.setattr(semantic, 'embed_documents', lambda texts: [np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts])
    index.ensure_node_index(corpus[0])
    while index.index_node_once(corpus[0]): pass
    return corpus


def test_index_is_bound_to_model_artifact_digest_and_windows(indexed):
    db = indexed[0]
    root = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == indexed[2].domain_id))
    row = db.scalar(select(DomainNodeEmbedding))
    assert row.artifact_id == root.id and row.model == semantic.MODEL_NAME
    assert row.text_sha256 == index.node_text(root)[2]
    assert index.node_index_status(db, root.id) == 'ready'
    assert db.get(DomainNodeIndexJob, root.id).attempts == 1
    index.ensure_node_index(db)
    assert not index.index_node_once(db)


def test_semantic_node_adds_original_candidate_and_shares_query_embedding(indexed, monkeypatch):
    calls = []
    def query(_):
        calls.append(True)
        return np.asarray([1, 0], dtype=np.float32)
    monkeypatch.setattr(semantic, 'embed_query', query)
    rows = search(indexed, query='unrelated-word')
    assert len(rows) == 1 and rows[0]['evidence_id'] == indexed[3].id
    assert rows[0]['knowledge_node']['match_type'] == 'semantic'
    assert rows[0]['match_type'] == 'semantic'
    assert rows[0]['snippet'] == indexed[3].text
    assert len(calls) == 1


def test_failed_local_model_keeps_keywords_and_does_not_retry_automatically(corpus, monkeypatch):
    build(corpus)
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
    monkeypatch.setattr(semantic, 'embed_documents', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic failure')))
    index.ensure_node_index(corpus[0]); assert index.index_node_once(corpus[0])
    job = corpus[0].scalar(select(DomainNodeIndexJob))
    assert job.status == 'failed' and job.last_error == 'RuntimeError'
    index.ensure_node_index(corpus[0]); assert not index.index_node_once(corpus[0])
    monkeypatch.setattr(semantic, 'embed_query', lambda *_: (_ for _ in ()).throw(RuntimeError('offline')))
    assert search(corpus)[0]['match_sources'] == ['knowledge_node']


def test_query_model_failure_and_bad_vectors_fall_back_to_node_keywords(indexed, monkeypatch):
    monkeypatch.setattr(semantic, 'embed_query', lambda *_: (_ for _ in ()).throw(RuntimeError('offline')))
    assert search(indexed)[0]['knowledge_node']['match_type'] == 'keyword'
    row = indexed[0].scalar(select(DomainNodeEmbedding)); row.vector = b'bad'; indexed[0].commit()
    monkeypatch.setattr(semantic, 'embed_query', lambda _: np.asarray([1, 0], dtype=np.float32))
    assert search(indexed)[0]['knowledge_node']['match_type'] == 'keyword'
    assert index.node_index_status(indexed[0], row.artifact_id) == 'pending'
    index.ensure_node_index(indexed[0]); assert index.index_node_once(indexed[0])
    assert index.node_index_status(indexed[0], row.artifact_id) == 'ready'


def test_source_change_prevents_using_cached_node_vector(indexed, monkeypatch):
    monkeypatch.setattr(semantic, 'embed_query', lambda _: np.asarray([1, 0], dtype=np.float32))
    indexed[2].title = 'Changed'; indexed[0].commit()
    assert not search(indexed, query='unrelated-word')


def test_agent_cannot_use_or_even_query_private_node_vectors(indexed, monkeypatch):
    monkeypatch.setattr(semantic, 'embed_query', lambda *_: pytest.fail('Private node triggered query inference'))
    assert not search(indexed, query='unrelated-word', agent=True, allowed={indexed[2].domain_id})


def test_inference_has_no_writer_lock_and_replacement_cannot_publish(indexed, monkeypatch):
    db, sessions = indexed[:2]
    job = db.scalar(select(DomainNodeIndexJob)); job.status = 'pending'; db.commit()
    old = job.artifact_id
    def during_inference(texts):
        with sessions() as other:
            state = locked_state(other, indexed[2].owner_id, indexed[2].domain_id, 'app')
            other.execute(delete(DomainSummaryArtifact).where(DomainSummaryArtifact.id == old))
            other.commit()
        return [np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts]
    monkeypatch.setattr(semantic, 'embed_documents', during_inference)
    assert index.index_node_once(db)
    assert db.get(DomainSummaryArtifact, old) is None
    assert db.scalar(select(DomainNodeEmbedding)) is None
    assert db.get(DomainNodeIndexJob, old) is None


def test_interrupted_local_job_can_resume_without_paid_tasks(indexed):
    db = indexed[0]
    job = db.scalar(select(DomainNodeIndexJob)); job.status = 'running'; db.commit()
    count = len(db.scalars(select(DomainSummaryJob.id)).all())
    index.interrupt_node_index(db); db.commit()
    assert index.index_node_once(db)
    assert len(db.scalars(select(DomainSummaryJob.id)).all()) == count


def test_owner_navigation_drills_to_child_and_originals(corpus):
    db, _, source = corpus[:3]
    parent = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='app', name='Parent', name_fixed=True)
    db.add(parent); db.flush()
    child = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='app', name='Child', parent_id=parent.id, name_fixed=True)
    db.add(child); db.flush()
    db.add(DomainTopicMember(topic_id=child.id, source_id=source.id, manual=True)); db.commit()
    analyzed(corpus); build(corpus)
    root = navigation.navigate(db, source.owner_id, source.domain_id, NavigationRequest())
    assert root['overview']['available'] and root['topics']['items'][0]['id'] == parent.id
    assert len(root['sources']['items']) == 1
    level1 = navigation.navigate(db, source.owner_id, source.domain_id, NavigationRequest(topic_id=parent.id))
    assert level1['topics']['items'][0]['id'] == child.id and level1['sources']['items'] == []
    level2 = navigation.navigate(db, source.owner_id, source.domain_id, NavigationRequest(topic_id=child.id))
    assert level2['topics']['items'] == [] and level2['sources']['items'][0]['id'] == source.id


def test_navigation_scope_pagination_and_owner_api(corpus):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.auth import require_app_principal
    add_documents(corpus, 2)
    corpus[2].visibility = 'app_only'; corpus[0].commit()
    app.dependency_overrides[get_db] = lambda: corpus[0]
    app.dependency_overrides[require_app_principal] = lambda: corpus[6]
    try:
        with TestClient(app) as client:
            path = f'/v1/domains/{corpus[2].domain_id}/analysis/navigation'
            first = client.post(path, json=dict(scope='shareable', limit=1)).json()
            assert first['sources']['items'][0]['id'] != corpus[2].id
            second = client.post(path, json=dict(scope='shareable', limit=1, source_offset=first['sources']['next_offset'])).json()
            assert second['sources']['items'][0]['id'] != first['sources']['items'][0]['id']
            assert second['sources']['next_offset'] is None
            assert client.post(path, json=dict(limit=0)).status_code == 422
            app.dependency_overrides[require_app_principal] = lambda: corpus[7]
            assert client.post(path, json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_owner_rebuild_can_retry_local_failure_without_paid_jobs(indexed, monkeypatch):
    from app.auth import AppPrincipal
    from app.domain_summary_jobs import NodeSelection
    db = indexed[0]
    job = db.scalar(select(DomainNodeIndexJob)); job.status = 'failed'; job.last_error = 'RuntimeError'; db.commit()
    before = db.scalars(select(DomainSummaryJob.id)).all()
    result = navigation.rebuild_node_index(indexed[2].domain_id, NodeSelection(), AppPrincipal(indexed[2].owner_id), db)
    assert result == dict(queued=1, status='pending')
    assert index.index_node_once(db)
    assert index.node_index_status(db, job.artifact_id) == 'ready'
    assert db.scalars(select(DomainSummaryJob.id)).all() == before


def test_cache_writes_do_not_dirty_domain_or_change_budget(indexed):
    db, _, source = indexed[:3]
    state = db.get(DomainProcessingState, (source.domain_id, 'app'))
    before = (state.generation, state.used_calls, state.used_input_tokens, state.dirty_since)
    job = db.scalar(select(DomainNodeIndexJob)); job.status = 'pending'; db.commit()
    assert index.index_node_once(db)
    db.refresh(state)
    assert (state.generation, state.used_calls, state.used_input_tokens, state.dirty_since) == before


def test_wrong_model_and_digest_do_not_contribute_semantic_hits(indexed, monkeypatch):
    monkeypatch.setattr(semantic, 'embed_query', lambda _: np.asarray([1, 0], dtype=np.float32))
    row = indexed[0].scalar(select(DomainNodeEmbedding)); row.text_sha256 = '0' * 64; indexed[0].commit()
    assert not search(indexed, query='unrelated-word')
    assert search(indexed)[0]['knowledge_node']['match_type'] == 'keyword'


def test_shared_navigation_hides_empty_private_topic(corpus):
    db, _, source = corpus[:3]
    topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='shareable', name='Private hint', name_fixed=True)
    db.add(topic); db.flush(); db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True))
    source.visibility = 'app_only'; db.commit()
    result = navigation.navigate(db, source.owner_id, source.domain_id, NavigationRequest(scope='shareable'))
    assert result['topics']['items'] == [] and result['sources']['items'] == []


def test_mixed_project_node_vector_is_filtered_before_query_inference(corpus, monkeypatch):
    add_documents(corpus, 1); build(corpus, scope='shareable')
    db, _, source, _, _, project = corpus[:6]
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
    monkeypatch.setattr(semantic, 'embed_documents', lambda texts: [np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts])
    index.ensure_node_index(db)
    while index.index_node_once(db): pass
    db.add(SourceProject(source_id=source.id, project_id=project.id)); db.commit()
    monkeypatch.setattr(semantic, 'embed_query', lambda *_: pytest.fail('Mixed-scope node vector was considered'))
    assert not search(corpus, query='unrelated-word', agent=True, allowed={source.domain_id}, project_id=project.id)


def test_new_root_during_inference_discards_old_claim_then_indexes_latest(indexed, monkeypatch):
    db, sessions = indexed[:2]
    job = db.scalar(select(DomainNodeIndexJob)); job.status = 'pending'; db.commit()
    old = job.artifact_id
    latest = str(uuid4())
    def replacement(texts):
        with sessions() as other:
            original = other.get(DomainSummaryArtifact, old)
            frozen, response = json.loads(original.snapshot_json), json.loads(original.response_json)
            frozen['request']['request_id'] = response['request_id'] = latest
            other.add(DomainSummaryArtifact(id=latest, owner_id=original.owner_id, domain_id=original.domain_id,
                scope=original.scope, node_id=original.node_id, snapshot_json=json.dumps(frozen), response_json=json.dumps(response)))
            other.commit()
        return [np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts]
    monkeypatch.setattr(semantic, 'embed_documents', replacement)
    assert index.index_node_once(db)
    index.ensure_node_index(db)
    assert db.get(DomainNodeIndexJob, old) is None
    assert db.scalar(select(DomainNodeEmbedding).where(DomainNodeEmbedding.artifact_id == old)) is None
    monkeypatch.setattr(semantic, 'embed_documents', lambda texts: [np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts])
    assert index.index_node_once(db)
    assert index.node_index_status(db, latest) == 'ready'


def test_owner_rebuild_never_reads_or_cleans_other_owners(indexed, monkeypatch):
    from app.auth import AppPrincipal
    from app.domain_summary_jobs import NodeSelection
    from app.models import Domain
    db = indexed[0]
    foreign = Domain(owner_id=indexed[7].owner_id, name='Foreign')
    db.add(foreign); db.flush()
    artifact = DomainSummaryArtifact(id=str(uuid4()), owner_id=foreign.owner_id, domain_id=foreign.id,
        node_id=foreign.id, scope='app', snapshot_json='{}', response_json='{}')
    db.add(artifact); db.flush()
    foreign_job = DomainNodeIndexJob(artifact_id=artifact.id, status='failed', model='Other model', text_sha256='0' * 64)
    db.add(foreign_job); db.commit()
    original = index.node_text
    def only_owned(row):
        assert row.owner_id == indexed[2].owner_id
        return original(row)
    monkeypatch.setattr(index, 'node_text', only_owned)
    navigation.rebuild_node_index(indexed[2].domain_id, NodeSelection(), AppPrincipal(indexed[2].owner_id), db)
    db.refresh(foreign_job)
    assert foreign_job.status == 'failed' and foreign_job.model == 'Other model'
