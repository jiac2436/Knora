"""Read-only domain tabs, using synthetic local data and mocked AI transport."""
from dataclasses import replace
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select

from app import domain_library as library, semantic, summary_index
from app.auth import require_app_principal
from app.ai import context_state
from app.database import get_db
from app.errors import ApiError
from app.main import app
from app.models import (AIArtifact, AIJob, DomainNodeIndexJob, DomainSummaryArtifact, DomainSummaryJob,
    DomainTopic, DomainTopicMember, FileAsset, KnowledgeTag, PersonalContext, Source, SourceImage, SourceRevision,
    TagAssignment, SemanticIndexJob, SummaryEmbedding, utcnow)
from test_domain_retrieval import build, corpus, prepared, storage, no_local_model
from test_domain_node_index import indexed
from test_summary_retrieval import analyzed


def add_source(db, first, title, **values):
    source = Source(owner_id=first.owner_id, domain_id=first.domain_id,
        title=title, kind='text', coverage='full_text', **values)
    db.add(source); db.flush()
    revision = SourceRevision(source_id=source.id, version=1, extracted_text='合成资料。')
    db.add(revision); db.flush(); source.current_revision_id = revision.id
    db.commit()
    return source


def topic(db, first, name, parent_id=None, **values):
    row = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id,
        scope='app', name=name, parent_id=parent_id, **values)
    db.add(row); db.commit()
    return row


def test_directory_subtree_deduplication_and_unclassified(storage):
    db, _, first = storage[:3]
    second = add_source(db, first, 'Second')
    third = add_source(db, first, 'Unclassified')
    parent = topic(db, first, 'Parent')
    child = topic(db, first, 'Child', parent.id)
    sibling = topic(db, first, 'Sibling', parent.id)
    hidden = topic(db, first, 'Empty automatic', automatic_key='tag:empty')
    db.add_all([DomainTopicMember(topic_id=parent.id, source_id=first.id),
        DomainTopicMember(topic_id=child.id, source_id=first.id),
        DomainTopicMember(topic_id=child.id, source_id=second.id),
        DomainTopicMember(topic_id=sibling.id, source_id=second.id)])
    db.commit()
    result = library.library_view(db, first.owner_id, first.domain_id, library.LibraryRequest())
    rows = {r['id']: r for r in result['items']}
    assert result['source_count'] == 3 and result['unclassified_count'] == 1
    assert result['topic_count'] == 3 and hidden.id not in rows
    assert rows[parent.id]['source_count'] == 2 and rows[parent.id]['direct_source_count'] == 1
    assert rows[child.id]['source_count'] == 2 and rows[sibling.id]['source_count'] == 1
    direct = library.library_sources_view(db, first.owner_id, first.domain_id,
        library.LibrarySourcesRequest(topic_id=parent.id))
    assert [r['id'] for r in direct['items']] == [first.id]
    unclassified = library.library_sources_view(db, first.owner_id, first.domain_id,
        library.LibrarySourcesRequest(unclassified=True))
    assert [r['id'] for r in unclassified['items']] == [third.id]


def test_pagination_and_scope_hide_deleted_and_foreign_sources(storage):
    db, _, first, _, target, _, _, other = storage
    second = add_source(db, first, 'Second')
    removed = add_source(db, first, 'Removed', deleted_at=utcnow())
    deleting = add_source(db, first, 'Deleting', processing_status='deleting')
    outside = add_source(db, first, 'Other domain'); outside.domain_id = target.id
    foreign = add_source(db, first, 'Other owner'); foreign.owner_id = other.owner_id
    db.commit()
    one = library.library_sources_view(db, first.owner_id, first.domain_id, library.LibrarySourcesRequest(limit=1))
    two = library.library_sources_view(db, first.owner_id, first.domain_id,
        library.LibrarySourcesRequest(limit=1, offset=one['next_offset']))
    assert one['total_count'] == 2 and two['next_offset'] is None
    assert {one['items'][0]['id'], two['items'][0]['id']} == {first.id, second.id}
    with pytest.raises(ApiError) as error:
        library.library_view(db, other.owner_id, first.domain_id, library.LibraryRequest())
    assert error.value.status_code == 404


def test_app_only_contract_and_owner_authentication(storage):
    db, _, source, _, _, _, owner, other = storage
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_app_principal] = lambda: owner
    base = f'/v1/domains/{source.domain_id}/analysis'
    try:
        with TestClient(app) as client:
            for endpoint in ('library', 'library/sources', 'information'):
                assert client.post(base + '/' + endpoint, json={}).status_code == 200
                assert client.post(base + '/' + endpoint, json={'scope': 'shareable'}).status_code == 422
            assert client.post(base + '/library/sources', json={'topic_id': 'x', 'unclassified': True}).status_code == 422
            assert client.post(base + '/library', json={'limit': 101}).status_code == 422
            app.dependency_overrides[require_app_principal] = lambda: other
            assert client.post(base + '/information', json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_information_current_analysis_and_tag_dual_origins(storage):
    artifact = analyzed(storage)
    db, _, source = storage[:3]
    manual = KnowledgeTag(owner_id=source.owner_id, name='Both', normalized_name='both', dimension='topic')
    stale = KnowledgeTag(owner_id=source.owner_id, name='Stale', normalized_name='stale', dimension='topic')
    db.add_all([manual, stale]); db.flush()
    db.add_all([TagAssignment(source_id=source.id, tag_id=manual.id, origin='manual'),
        TagAssignment(source_id=source.id, tag_id=manual.id, origin='ai', revision_id=source.current_revision_id),
        TagAssignment(source_id=source.id, tag_id=stale.id, origin='ai', revision_id='old')]); db.commit()
    result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert result['documents'] == dict(total=1, analyzed=1, pending=0, outdated=0, running=0, failed=0)
    assert result['tags'] == dict(total=1, manual=1, ai=1, both=1,
        items=[dict(id=manual.id, name='Both', origins=['ai', 'manual'])], next_offset=None)
    assert result['rag']['layers']['original']['disabled'] == 1
    assert result['rag']['layers']['summary']['disabled'] == 1
    source.title = 'Changed'; db.commit()
    stale_result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert stale_result['documents']['outdated'] == 1 and stale_result['documents']['analyzed'] == 0
    assert stale_result['rag']['layers']['summary']['unavailable'] == 1


def test_document_status_is_unique_and_failed_unknown_not_pending(storage):
    db, _, source = storage[:3]
    pending = add_source(db, source, 'Pending')
    running = add_source(db, source, 'Running')
    failed = add_source(db, source, 'Unknown')
    for item, status in ((running, 'running'), (failed, 'unknown')):
        db.add(AIJob(owner_id=item.owner_id, source_id=item.id, kind='analyze', status=status,
            request_key=str(uuid4()), request_sha256='0' * 64,
            snapshot_json=json.dumps(dict(revision_id=item.current_revision_id, context=context_state(db, item)))))
    db.commit()
    result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert result['documents'] == dict(total=4, analyzed=0, pending=2, outdated=0, running=1, failed=1)


@pytest.mark.parametrize('status', ['failed', 'running', 'pending'])
@pytest.mark.parametrize('change', ['revision', 'context'])
def test_old_task_snapshot_does_not_mark_changed_source_running_or_failed(storage, status, change):
    db, _, source = storage[:3]
    db.add(AIJob(owner_id=source.owner_id, source_id=source.id, kind='analyze', status=status,
        request_key=str(uuid4()), request_sha256='0' * 64,
        snapshot_json=json.dumps(dict(revision_id=source.current_revision_id, context=context_state(db, source)))))
    db.commit()
    before = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert before['documents']['failed' if status == 'failed' else 'running'] == 1
    if change == 'revision':
        revision = SourceRevision(source_id=source.id, version=2, extracted_text='已经修改正文。')
        db.add(revision); db.flush(); source.current_revision_id = revision.id
    else:
        db.add(PersonalContext(source_id=source.id, notes='已经修改备注。'))
    db.commit()
    after = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert after['documents'] == dict(total=1, analyzed=0, pending=1, outdated=0, running=0, failed=0)


def test_old_failed_task_with_historical_artifact_reports_outdated(storage):
    analyzed(storage)
    db, _, source = storage[:3]
    db.add(AIJob(owner_id=source.owner_id, source_id=source.id, kind='analyze', status='failed',
        request_key=str(uuid4()), request_sha256='0' * 64,
        snapshot_json=json.dumps(dict(revision_id=source.current_revision_id, context=context_state(db, source)))))
    db.add(PersonalContext(source_id=source.id, notes='分析之后修改的备注。'))
    db.commit()
    after = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert after['documents'] == dict(total=1, analyzed=0, pending=0, outdated=1, running=0, failed=0)


def test_storage_reports_scoped_logical_bytes_never_physical_database_size(storage):
    db, _, source, _, target = storage[:5]
    outside = add_source(db, source, 'Outside'); outside.domain_id = target.id
    db.add_all([FileAsset(source_id=source.id, filename='test.pdf', size_bytes=125, sha256='0' * 64),
        FileAsset(source_id=outside.id, filename='outside.pdf', size_bytes=999, sha256='0' * 64),
        SourceImage(id=str(uuid4()), source_id=source.id, revision_id=source.current_revision_id,
            ordinal=0, status='saved', size_bytes=50)])
    db.commit()
    result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    size = result['storage']
    assert size['recorded_asset_bytes'] == 175
    assert size['text_utf8_bytes'] == len('Synthetic evidence'.encode())
    assert size['total_bytes'] is None and size['physical_bytes'] is None
    assert size['measurement'] == 'logical_partial'
    assert size['known_payload_bytes'] == 175 + size['text_utf8_bytes']


def test_citations_return_current_original_and_reject_uncited_ids(corpus):
    build(corpus)
    db, _, source, evidence = corpus[:4]
    artifact = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    frozen = json.loads(artifact.snapshot_json)
    citation = json.loads(artifact.response_json)['result']['overview']['citations'][0]
    result = library.citations_view(db, source.owner_id, source.domain_id, artifact.id,
        library.CitationsRequest(citation_ids=[citation]))
    item = result['items'][0]
    assert item['source_id'] == source.id and item['revision_id'] == source.current_revision_id
    assert item['evidence_id'] == evidence.id and item['text'] == evidence.text
    assert item['text_start'] == 0 and item['text_end'] == len(evidence.text)
    with pytest.raises(ApiError) as error:
        library.citations_view(db, source.owner_id, source.domain_id, artifact.id,
            library.CitationsRequest(citation_ids=['not-cited']))
    assert error.value.status_code == 404


@pytest.mark.parametrize('locator_kind', ['text', 'pdf_page'])
def test_window_citations_preserve_chunk_and_document_or_page_coordinates(corpus, locator_kind):
    db, _, source, evidence = corpus[:4]
    text = '合成原文。' * 500
    evidence.text = text
    evidence.locator_kind = locator_kind
    evidence.locator_start = 500 if locator_kind == 'text' else 7
    evidence.locator_end = 500 + len(text) if locator_kind == 'text' else 7
    db.get(SourceRevision, source.current_revision_id).extracted_text = '前' * 500 + text
    db.commit()
    single = analyzed(corpus)
    snapshot = json.loads(single.snapshot_json)
    entry = snapshot['evidence'][0]
    entry.update(text=text[37:2037], text_start=37, text_end=2037)
    single.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    db.commit()
    build(corpus)
    artifact = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    citations = json.loads(artifact.response_json)['result']['overview']['citations']
    result = library.citations_view(db, source.owner_id, source.domain_id, artifact.id,
        library.CitationsRequest(citation_ids=citations))
    assert [r['text_start'] for r in result['items']] == [37, 1037]
    for item in result['items']:
        assert item['text'] == text[item['text_start']:item['text_end']]
        if locator_kind == 'text':
            assert item['locator_start'] == 500 + item['text_start']
            assert item['locator_end'] == 500 + item['text_end']
        else:
            assert item['locator_start'] == item['locator_end'] == 7


@pytest.mark.parametrize('change', ['title', 'revision', 'deleted'])
def test_citations_never_serve_stale_original(corpus, change):
    build(corpus)
    db, _, source = corpus[:3]
    artifact = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    citation = json.loads(artifact.response_json)['result']['overview']['citations'][0]
    if change == 'title': source.title = 'Updated'
    elif change == 'deleted': source.deleted_at = utcnow()
    else: source.current_revision_id = None
    db.commit()
    with pytest.raises(ApiError) as error:
        library.citations_view(db, source.owner_id, source.domain_id, artifact.id,
            library.CitationsRequest(citation_ids=[citation]))
    assert error.value.status_code in (404, 409)


def test_information_shares_manifest_and_marks_invalidated_nodes_unavailable(corpus, monkeypatch):
    build(corpus)
    db, _, source = corpus[:3]
    calls = []
    original = library.NodeQuerySnapshot
    def snapshot(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(library, 'NodeQuerySnapshot', snapshot)
    result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert len(calls) == 1 and result['overview_coverage'] is not None
    assert result['rag']['layers']['domain']['disabled'] == 1
    source.title = 'Updated'; db.commit()
    changed = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert changed['overview_coverage'] is None and changed['rag']['layers']['domain']['unavailable'] == 1


def test_semantic_layers_report_current_scoped_indexes_only(indexed):
    db, _, source = indexed[:3]
    semantic.enqueue_semantic_index(db, source); db.commit()
    assert semantic.index_one(db)
    summary_index.ensure_summary_index(db)
    assert summary_index.index_summary_once(db)
    result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert result['rag']['semantic_enabled'] and result['rag']['keyword_available']
    for name in ('original', 'summary', 'domain'):
        assert result['rag']['layers'][name]['ready'] == 1
        assert result['rag']['layers'][name]['chunk_count'] > 0
    db.get(SemanticIndexJob, source.id).revision_id = 'old'
    db.scalar(select(SummaryEmbedding)).revision_id = 'old'
    db.commit()
    changed = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert changed['rag']['layers']['original']['pending'] == 1
    assert changed['rag']['layers']['summary']['pending'] == 1
    assert changed['rag']['layers']['summary']['chunk_count'] == 0


def test_many_published_nodes_share_one_manifest_load(corpus, monkeypatch):
    from app import domain_query_snapshot
    db, _, source = corpus[:3]
    first = topic(db, source, 'First')
    second = topic(db, source, 'Second')
    db.add_all([DomainTopicMember(topic_id=first.id, source_id=source.id),
        DomainTopicMember(topic_id=second.id, source_id=source.id)])
    db.commit()
    build(corpus, topic_id=first.id); build(corpus, topic_id=second.id); build(corpus)
    calls = []
    original = domain_query_snapshot.freeze_domain_inputs
    def freeze(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(domain_query_snapshot, 'freeze_domain_inputs', freeze)
    result = library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    assert len(calls) == 1
    assert result['rag']['layers']['topics']['disabled'] == 2
    assert result['rag']['layers']['domain']['disabled'] == 1


def test_reads_create_no_jobs_or_authorizations_and_execute_no_writes(corpus):
    build(corpus)
    db, _, source = corpus[:3]
    before = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
              for model in (AIJob, DomainSummaryJob, DomainNodeIndexJob)}
    writes = []
    def observe(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().split(None, 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE'):
            writes.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', observe)
    try:
        library.library_view(db, source.owner_id, source.domain_id, library.LibraryRequest())
        library.library_sources_view(db, source.owner_id, source.domain_id, library.LibrarySourcesRequest())
        library.information_view(db, source.owner_id, source.domain_id, library.InformationRequest())
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', observe)
    assert not writes
    assert before == {model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (AIJob, DomainSummaryJob, DomainNodeIndexJob)}
