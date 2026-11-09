"""Owner-only processing trees stay readable without runtime or paid calls."""
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, select

from app.auth import require_app_principal
from app.database import get_db
from app.domain_processing_nodes import processing_view
from app.domain_summary_jobs import current_node_result
from app.errors import ApiError
from app.main import app
from app.models import DomainBuild, DomainSummaryArtifact, DomainSummaryJob, Source
from test_ai_analysis import storage
from test_domain_summary_jobs import prepared
from test_domain_builds import corpus, add_documents, enqueue, finish
from test_domain_topic_mapping import make_topic


@pytest.fixture
def processing_tree(corpus):
    add_documents(corpus, 20)
    db, _, first = corpus[:3]
    sources = db.scalars(select(Source).where(Source.domain_id == first.domain_id)).all()
    topic = make_topic(corpus, sources)
    queued, _ = enqueue(corpus)
    finish(corpus, [])
    assert queued.status == 'completed'
    root_id = current_node_result(db, first.owner_id, first.domain_id, 'app')['id']
    return corpus, topic, root_id


def read(data, identifier, *, owner=None, domain=None, scope='app'):
    db, _, source = data[:3]
    return processing_view(db, owner or source.owner_id, domain or source.domain_id, scope, identifier)


def paid_records(db):
    return (set(db.scalars(select(DomainBuild.id))), set(db.scalars(select(DomainSummaryJob.id))))


def copy_artifact(db, original, *, scope=None, children=None, identifier=None):
    identifier = identifier or str(uuid4())
    frozen = json.loads(original.snapshot_json)
    response = json.loads(original.response_json)
    frozen['request']['request_id'] = response['request_id'] = identifier
    if children is not None:
        frozen['child_artifact_ids'] = children
    clone = DomainSummaryArtifact(id=identifier, owner_id=original.owner_id, domain_id=original.domain_id,
        scope=scope or original.scope, node_id=original.node_id,
        snapshot_json=json.dumps(frozen), response_json=json.dumps(response))
    db.add(clone)
    db.commit()
    return clone


def test_processing_tree_descends_from_domain_to_complete_topic_and_original_batches(processing_tree):
    data, topic, root_id = processing_tree
    before = paid_records(data[0])
    root = read(data, root_id)
    assert root['available'] and root['node_id'] == data[2].domain_id
    assert root['original_evidence_required'] is True
    assert len(root['children']) == 1
    branch = read(data, root['children'][0]['artifact_id'])
    assert branch['available'] and branch['published_topic_id'] == topic.id
    assert branch['coverage']['processed_source_count'] == 21
    assert len(branch['children']) == 2
    pending, leaves, visited = list(branch['children']), [], set()
    while pending:
        child = pending.pop()
        assert child['artifact_id'] not in visited
        visited.add(child['artifact_id'])
        node = read(data, child['artifact_id'])
        assert node['available']
        if node['children']:
            pending.extend(node['children'])
        else:
            leaves.append(node)
    assert len(leaves) == 3
    assert sum(leaf['coverage']['processed_source_count'] for leaf in leaves) == 21
    assert all(leaf['original_evidence_required'] for leaf in leaves)
    assert paid_records(data[0]) == before


def test_runtime_build_and_job_removal_does_not_break_processing_reads(processing_tree):
    data, _, root_id = processing_tree
    db = data[0]
    db.execute(delete(DomainSummaryJob))
    db.execute(delete(DomainBuild))
    db.commit()
    root = read(data, root_id)
    assert root['available']
    branch = read(data, root['children'][0]['artifact_id'])
    assert branch['available']
    assert all(read(data, child['artifact_id'])['available'] for child in branch['children'])
    assert paid_records(db) == (set(), set())


@pytest.mark.parametrize('mismatch', ['owner', 'domain', 'scope', 'identifier'])
def test_processing_node_identity_and_scope_fail_closed(processing_tree, mismatch):
    data, _, root_id = processing_tree
    options = {}
    if mismatch == 'owner': options['owner'] = data[7].owner_id
    elif mismatch == 'domain': options['domain'] = data[4].id
    elif mismatch == 'scope': options['scope'] = 'shareable'
    else: root_id = str(uuid4())
    with pytest.raises(ApiError) as error:
        read(data, root_id, **options)
    assert error.value.status_code == 404


@pytest.mark.parametrize('child_kind', ['missing', 'self', 'cross_scope'])
def test_corrupt_child_references_never_return_parent_model_text(processing_tree, child_kind):
    data, _, root_id = processing_tree
    db = data[0]
    root = db.get(DomainSummaryArtifact, root_id)
    identifier = str(uuid4())
    if child_kind == 'missing':
        child_id = str(uuid4())
    elif child_kind == 'self':
        child_id = identifier
    else:
        branch_id = json.loads(root.snapshot_json)['child_artifact_ids'][0]
        child_id = copy_artifact(db, db.get(DomainSummaryArtifact, branch_id), scope='shareable').id
    clone = copy_artifact(db, root, children=[child_id], identifier=identifier)
    before = paid_records(db)
    result = read(data, clone.id)
    assert result == dict(available=False, reason='invalid_children')
    assert 'result' not in result and 'children' not in result
    assert paid_records(db) == before


def test_changed_original_invalidates_domain_topic_and_containing_leaf(processing_tree):
    data, _, root_id = processing_tree
    root = read(data, root_id)
    branch_id = root['children'][0]['artifact_id']
    branch = read(data, branch_id)
    db, _, first = data[:3]
    leaf_id = next(child['artifact_id'] for child in branch['children']
        if first.id in {item['source_id'] for item in json.loads(
            db.get(DomainSummaryArtifact, child['artifact_id']).snapshot_json)['processing_manifest']['sources']})
    first.title = '加工后修改原文输入'
    db.commit()
    before = paid_records(db)
    for identifier in (root_id, branch_id, leaf_id):
        result = read(data, identifier)
        assert result['available'] is False and 'result' not in result
    assert paid_records(db) == before


def test_owner_processing_api_drills_down_without_creating_paid_work(processing_tree):
    data, topic, root_id = processing_tree
    db, _, first = data[:3]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_app_principal] = lambda: data[6]
    before = paid_records(db)
    base = f'/v1/domains/{first.domain_id}/analysis/processing-nodes/'
    try:
        with TestClient(app) as http:
            response = http.post(base + root_id, json={})
            assert response.status_code == 200, response.text
            root = response.json()
            branch_response = http.post(base + root['children'][0]['artifact_id'], json={})
            assert branch_response.status_code == 200 and branch_response.json()['published_topic_id'] == topic.id
            for child in branch_response.json()['children']:
                leaf = http.post(base + child['artifact_id'], json={})
                assert leaf.status_code == 200 and leaf.json()['available']
            assert http.post(base + root_id, json={'scope': 'shareable'}).status_code == 404
            assert http.post(base + root_id, json={'topic_id': topic.id}).status_code == 422
            app.dependency_overrides[require_app_principal] = lambda: data[7]
            assert http.post(base + root_id, json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()
    assert paid_records(db) == before
