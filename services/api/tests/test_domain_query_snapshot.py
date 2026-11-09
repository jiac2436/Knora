"""Permission-gated inputs are reused only within one unchanged query transaction."""
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app import domain_query_snapshot as snapshots, domain_retrieval, domain_summary_jobs as jobs
from app.domain_inputs import freeze_domain_inputs, manifest_fingerprint
from app.domain_runtime import locked_state
from app.domain_topics import source_scope
from app.errors import ApiError
from app.models import Domain, DomainTopicMember, EvidenceChunk, PersonalContext, Source, SourceProject, SourceRevision
from test_ai_analysis import storage
from test_domain_summary_jobs import prepared
from test_domain_builds import corpus, add_documents
from test_domain_retrieval import build, no_local_model
from test_domain_topic_mapping import make_topic


def inputs(data, *, narrow=None):
    db, _, first = data[:3]
    scope = source_scope(first.owner_id, first.domain_id, 'app')
    if narrow:
        scope = scope.where(Source.id.in_(narrow))
    locked_state(db, first.owner_id, first.domain_id, 'app')
    artifacts = list(db.scalars(domain_retrieval.eligible_nodes(first.owner_id, scope, False)))
    return snapshots.NodeQuerySnapshot(db, first.owner_id, scope, artifacts), scope, artifacts


def hits(data):
    first = data[2]
    return domain_retrieval.node_hits(data[0], first.owner_id, 'lattice', ['lattice'],
        source_scope(first.owner_id, first.domain_id, 'app'))


def test_multiple_topic_and_root_nodes_freeze_each_dependency_union_once(corpus, monkeypatch):
    add_documents(corpus, 2)
    db, _, first = corpus[:3]
    sources = list(db.scalars(select(Source).where(Source.domain_id == first.domain_id).order_by(Source.id)))
    make_topic(corpus, sources[:2], name='左'); make_topic(corpus, sources[1:], name='右')
    build(corpus)
    calls = []
    original = snapshots.freeze_domain_inputs
    def freeze(*args, **kwargs):
        calls.append(kwargs['allowed_source_ids'])
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshots, 'freeze_domain_inputs', freeze)
    rows = hits(corpus)
    assert rows and len(calls) == 1 and calls[0] == frozenset(s.id for s in sources)
    assert all(value[-1]['role'] == 'retrieval_hint' for value in rows.values())
    assert all(value[1].revision_id == value[2].id for value in rows.values())


def test_subset_hashes_match_uncached_full_input_contract(corpus):
    add_documents(corpus, 2)
    db, _, first = corpus[:3]
    sources = list(db.scalars(select(Source).where(Source.domain_id == first.domain_id).order_by(Source.id)))
    left = make_topic(corpus, sources[:2], name='左'); right = make_topic(corpus, sources[1:], name='右')
    db.add(DomainTopicMember(topic_id=left.id, source_id=sources[-1].id, included=False, manual=True)); db.commit()
    build(corpus)
    context, _, _ = inputs(corpus)
    for ids in [frozenset(s.id for s in sources), frozenset(s.id for s in sources[:2]), frozenset([sources[-1].id])]:
        cached = context.manifest(first.owner_id, first.domain_id, 'app', allowed_source_ids=ids)
        expected = freeze_domain_inputs(db, first.owner_id, first.domain_id, scope='app', allowed_source_ids=ids)
        assert manifest_fingerprint(cached) == manifest_fingerprint(expected)


@pytest.mark.parametrize('change', ['title', 'raw_input', 'revision_text', 'context', 'project', 'domain_name'])
def test_complete_input_hash_still_invalidates_changed_metadata(corpus, change):
    build(corpus)
    db, _, source, _, _, project = corpus[:6]
    if change == 'title': source.title = 'Edited'
    elif change == 'raw_input': source.raw_input = 'Edited'
    elif change == 'revision_text': db.get(SourceRevision, source.current_revision_id).extracted_text = 'Edited'
    elif change == 'context': db.add(PersonalContext(source_id=source.id, notes='Edited'))
    elif change == 'project': db.add(SourceProject(source_id=source.id, project_id=project.id))
    elif change == 'domain_name': db.get(Domain, source.domain_id).name = 'Edited'
    db.commit()
    assert hits(corpus) == {}


@pytest.mark.parametrize('change', ['orm', 'sql', 'commit', 'rollback'])
def test_mid_query_change_discards_already_accumulated_node_results(corpus, monkeypatch, change):
    build(corpus)
    db, _, source = corpus[:3]
    original = domain_retrieval.resolve_original
    mutated = []
    def resolve(*args, **kwargs):
        result = original(*args, **kwargs)
        if not mutated:
            mutated.append(True)
            if change == 'orm': source.visibility = 'app_only'
            elif change == 'sql': db.execute(update(Source).where(Source.id == source.id).values(visibility='app_only'))
            elif change == 'commit': db.commit()
            else: db.rollback()
        return result
    monkeypatch.setattr(domain_retrieval, 'resolve_original', resolve)
    assert hits(corpus) == {} and mutated


def test_context_cannot_be_reused_in_next_transaction_or_owner_scope(corpus):
    build(corpus)
    context, scope, artifacts = inputs(corpus)
    db, _, first = corpus[:3]
    with pytest.raises(ApiError): context.source(corpus[7].owner_id, first.id, scope)
    with pytest.raises(ApiError): context.source(first.owner_id, first.id, source_scope(first.owner_id, first.domain_id, 'app'))
    db.commit()
    with pytest.raises(ApiError): jobs.current_node_result(db, first.owner_id, first.domain_id, 'app',
        validation_context=context, artifact=artifacts[0])


def test_narrow_gate_never_freezes_outside_project_sources(corpus, monkeypatch):
    add_documents(corpus, 1)
    db, _, first = corpus[:3]
    make_topic(corpus, [first], name='限定主题')
    build(corpus)
    expected = frozenset([first.id]); calls = []
    original = snapshots.freeze_domain_inputs
    def freeze(*args, **kwargs):
        calls.append(kwargs['allowed_source_ids'])
        assert kwargs['allowed_source_ids'] == expected
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshots, 'freeze_domain_inputs', freeze)
    scope = source_scope(first.owner_id, first.domain_id, 'app').where(Source.id == first.id)
    rows = domain_retrieval.node_hits(db, first.owner_id, 'lattice', ['lattice'], scope)
    assert set(rows) == {first.id} and calls == [expected]


def test_new_topic_member_outside_gated_union_fails_closed(corpus, monkeypatch):
    topic = make_topic(corpus)
    build(corpus)
    add_documents(corpus, 1)
    db, _, first = corpus[:3]
    added = db.scalar(select(Source).where(Source.domain_id == first.domain_id, Source.id != first.id))
    db.add(DomainTopicMember(topic_id=topic.id, source_id=added.id, manual=True)); db.commit()
    calls = []
    original = snapshots.freeze_domain_inputs
    def freeze(*args, **kwargs):
        calls.append(kwargs['allowed_source_ids'])
        assert added.id not in kwargs['allowed_source_ids']
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshots, 'freeze_domain_inputs', freeze)
    assert hits(corpus) == {} and calls


def test_nonmatching_nodes_do_not_freeze_input_union(corpus, monkeypatch):
    build(corpus)
    monkeypatch.setattr(snapshots, 'freeze_domain_inputs', lambda *a, **k: pytest.fail('Unmatched nodes should not freeze'))
    first = corpus[2]
    assert domain_retrieval.node_hits(corpus[0], first.owner_id, 'unrelated', ['unrelated'],
        source_scope(first.owner_id, first.domain_id, 'app')) == {}



def test_snapshot_without_transaction_fails_closed(corpus):
    db, _, first = corpus[:3]
    owner, domain = first.owner_id, first.domain_id
    db.rollback()
    with pytest.raises(ApiError) as error:
        snapshots.NodeQuerySnapshot(db, owner, source_scope(owner, domain, 'app'), [])
    assert error.value.code == 'query_snapshot_changed'


def test_multiple_domains_keep_separate_input_unions_under_sqlite_lock(corpus, monkeypatch):
    from test_summary_retrieval import analyzed
    db, _, first, _, target = corpus[:5]
    source = Source(owner_id=first.owner_id, domain_id=target.id, title='其他域', kind='text', coverage='full_text')
    db.add(source); db.flush()
    revision = SourceRevision(source_id=source.id, version=1, extracted_text='另一个域的原文')
    db.add(revision); db.flush(); source.current_revision_id = revision.id
    evidence = EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text, locator_end=len(revision.extracted_text))
    db.add(evidence); db.commit()
    second = list(corpus); second[2:4] = source, evidence; second = tuple(second)
    analyzed(second, summary='其他域的方法')
    make_topic(corpus, name='同名主题'); make_topic(second, name='同名主题')
    build(corpus); build(second)
    calls = []
    original = snapshots.freeze_domain_inputs
    def freeze(db, owner, domain, **kwargs):
        calls.append((domain, kwargs['allowed_source_ids']))
        return original(db, owner, domain, **kwargs)
    monkeypatch.setattr(snapshots, 'freeze_domain_inputs', freeze)
    scope = select(Source).where(Source.owner_id == first.owner_id, Source.deleted_at.is_(None),
        Source.processing_status.not_in(('deleting', 'deleted')))
    rows = domain_retrieval.node_hits(db, first.owner_id, 'lattice', ['lattice'], scope)
    assert set(rows) == {first.id, source.id}
    assert set(calls) == {(first.domain_id, frozenset([first.id])), (source.domain_id, frozenset([source.id]))}


def test_corrupt_matching_node_does_not_suppress_other_valid_nodes(corpus):
    from app.models import DomainSummaryArtifact
    add_documents(corpus, 1)
    db, _, first = corpus[:3]
    sources = list(db.scalars(select(Source).where(Source.domain_id == first.domain_id).order_by(Source.id)))
    left = make_topic(corpus, sources[:1], name='左'); make_topic(corpus, sources[1:], name='右')
    build(corpus)
    artifact = next(row for row in db.scalars(select(DomainSummaryArtifact))
                    if json.loads(row.snapshot_json).get('published_topic_id') == left.id)
    frozen = json.loads(artifact.snapshot_json); frozen['topic_manifest'] = None
    identifier = str(uuid4())
    frozen['request']['request_id'] = identifier
    response = json.loads(artifact.response_json); response['request_id'] = identifier
    db.add(DomainSummaryArtifact(id=identifier, owner_id=artifact.owner_id, domain_id=artifact.domain_id,
        scope=artifact.scope, node_id=artifact.node_id, snapshot_json=json.dumps(frozen), response_json=json.dumps(response)))
    db.commit()
    assert set(hits(corpus)) == {source.id for source in sources}


def test_bulk_rows_preserve_rich_input_payload_and_tag_origins(corpus):
    from app import ai
    from app.models import ExperienceRecord, KnowledgeTag, Project, TagAssignment
    from app.summary_retrieval import source_tags, source_tags_many
    from test_summary_retrieval import analyzed
    db, _, source, _, _, project = corpus[:6]
    project.goal, project.constraints, project.background = '目标', '约束', '背景'
    second_project = Project(owner_id=source.owner_id, name='次项目', goal='其他目标')
    db.add(second_project); db.flush()
    db.add_all([SourceProject(source_id=source.id, project_id=project.id),
        SourceProject(source_id=source.id, project_id=second_project.id),
        PersonalContext(source_id=source.id, reason='保存原因', notes='人工笔记', applicability='边界', version=7),
        ExperienceRecord(owner_id=source.owner_id, source_id=source.id, project_id=project.id, result='success', notes='实践结果')])
    tag = KnowledgeTag(owner_id=source.owner_id, name='手工与AI', normalized_name='手工与ai', dimension='topic')
    stale = KnowledgeTag(owner_id=source.owner_id, name='过期AI', normalized_name='过期ai', dimension='topic')
    db.add_all([tag, stale]); db.flush()
    db.add_all([TagAssignment(source_id=source.id, tag_id=tag.id, origin='manual'),
        TagAssignment(source_id=source.id, tag_id=tag.id, origin='ai', revision_id=source.current_revision_id),
        TagAssignment(source_id=source.id, tag_id=stale.id, origin='ai', revision_id=str(uuid4()))])
    db.commit(); analyzed(corpus)
    make_topic(corpus); build(corpus)
    context, _, _ = inputs(corpus)
    original = freeze_domain_inputs(db, source.owner_id, source.domain_id, scope='app')
    assert context.inputs[(source.domain_id, 'app')].model_dump() == original.model_dump()
    assert context.rows.context_state(source) == ai.context_state(db, source)
    assert source_tags_many(db, source.owner_id, [source.id])[source.id] == source_tags(db, source)
    tags = context.rows.tags[source.id]
    assert len(tags) == 1 and tags[0]['origins'] == ['ai', 'manual']


@pytest.mark.parametrize('invalid', ['json', 'context', 'owner', 'revision', 'citations'])
def test_preloaded_summary_candidates_retain_all_current_validation(storage, invalid):
    from types import SimpleNamespace
    from app import ai
    from app.summary_index import current_summary_artifact
    from test_summary_retrieval import analyzed
    valid = analyzed(storage)
    db, _, source = storage[:3]
    fields = dict(id=str(uuid4()), source_id=valid.source_id, owner_id=valid.owner_id,
        revision_id=valid.revision_id, snapshot_json=valid.snapshot_json, result_json=valid.result_json)
    if invalid == 'json': fields['result_json'] = 'malformed'
    elif invalid == 'context':
        snapshot = json.loads(fields['snapshot_json']); snapshot['context']['title'] = 'stale'
        fields['snapshot_json'] = json.dumps(snapshot)
    elif invalid == 'owner': fields['owner_id'] = storage[7].owner_id
    elif invalid == 'revision': fields['revision_id'] = str(uuid4())
    elif invalid == 'citations':
        result = json.loads(fields['result_json']); result['summary_citations'] = ['missing']
        fields['result_json'] = json.dumps(result)
    current_context = ai.context_state(db, source)
    assert current_summary_artifact(db, source, candidates=[SimpleNamespace(**fields)], current_context=current_context) is None
    assert current_summary_artifact(db, source, candidates=[SimpleNamespace(**fields), valid], current_context=current_context).id == valid.id



def test_pending_orm_edit_survives_autoflush_disabled_query(corpus):
    build(corpus)
    db, _, source = corpus[:3]
    db.autoflush = False
    source.title = '尚未保存的新标题'
    assert source in db.dirty
    assert hits(corpus) == {}
    assert source.title == '尚未保存的新标题' and source in db.dirty


def newer_artifact(db, artifact, *, identifier=None, snapshot=None, node_id=None, created_at=None):
    from app.models import DomainSummaryArtifact
    identifier = identifier or str(uuid4())
    frozen = snapshot if snapshot is not None else json.loads(artifact.snapshot_json)
    if isinstance(frozen, dict):
        frozen['request']['request_id'] = identifier
        frozen['request']['node_id'] = node_id or artifact.node_id
        frozen = json.dumps(frozen)
    response = json.loads(artifact.response_json); response['request_id'] = identifier
    row = DomainSummaryArtifact(id=identifier, owner_id=artifact.owner_id, domain_id=artifact.domain_id,
        scope=artifact.scope, node_id=node_id or artifact.node_id, snapshot_json=frozen, response_json=json.dumps(response))
    if created_at is not None:
        row.created_at = created_at
    db.add(row); db.commit()
    return row


def test_newer_private_dependency_suppresses_old_visible_node_before_output_read(corpus, monkeypatch):
    from app.models import DomainSummaryArtifact
    build(corpus, scope='shareable')
    db, _, source = corpus[:3]
    old = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id,
        DomainSummaryArtifact.scope == 'shareable'))
    private = Source(owner_id=source.owner_id, domain_id=source.domain_id, title='私有内容',
        kind='text', coverage='full_text', visibility='app_only')
    db.add(private); db.flush()
    frozen = json.loads(old.snapshot_json)
    frozen['global_manifest']['sources'].append(dict(source_id=private.id, revision_id=None,
        summary_artifact_id=None, input_sha256='0' * 64))
    newer_artifact(db, old, snapshot=frozen)
    monkeypatch.setattr(domain_retrieval.DomainSummaryResponse, 'model_validate_json',
        lambda *a, **k: pytest.fail('Inaccessible latest node must not revive old generated output'))
    assert domain_retrieval.node_hits(db, source.owner_id, 'lattice', ['lattice'],
        source_scope(source.owner_id, source.domain_id, 'shareable'), for_agent=True) == {}


def test_mapped_and_explicit_topic_share_latest_namespace_and_uuid_tie_rule(corpus):
    from app.models import DomainSummaryArtifact
    topic = make_topic(corpus); build(corpus)
    db, _, source = corpus[:3]
    mapped = next(row for row in db.scalars(select(DomainSummaryArtifact))
                  if json.loads(row.snapshot_json).get('published_topic_id') == topic.id)
    timestamp = mapped.created_at
    explicit = newer_artifact(db, mapped, identifier='ffffffff-ffff-ffff-ffff-fffffffffffd',
        node_id=topic.id, created_at=timestamp)
    scope = source_scope(source.owner_id, source.domain_id, 'app')
    rows = {row.id for row in db.scalars(domain_retrieval.eligible_nodes(source.owner_id, scope, False))}
    assert explicit.id in rows and mapped.id not in rows
    latest = newer_artifact(db, mapped, identifier='ffffffff-ffff-ffff-ffff-fffffffffffe',
        node_id='latest-processing-branch', created_at=timestamp)
    rows = {row.id for row in db.scalars(domain_retrieval.eligible_nodes(source.owner_id, scope, False))}
    assert latest.id in rows and mapped.id not in rows and explicit.id not in rows


def test_invalid_json_latest_snapshot_cannot_revive_old_root(corpus, monkeypatch):
    from app.models import DomainSummaryArtifact
    build(corpus)
    db, _, source = corpus[:3]
    old = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    newer_artifact(db, old, snapshot='not JSON')
    monkeypatch.setattr(domain_retrieval.DomainSummaryResponse, 'model_validate_json',
        lambda *a, **k: pytest.fail('Invalid latest snapshot must suppress old generated output'))
    assert hits(corpus) == {}



@pytest.mark.parametrize('alias', ['', 0])
def test_falsey_json_alias_is_not_mistaken_for_domain_root(corpus, alias):
    from app.models import DomainSummaryArtifact
    build(corpus)
    db, _, source = corpus[:3]
    old = db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == source.domain_id))
    frozen = json.loads(old.snapshot_json); frozen['published_topic_id'] = alias
    newer_artifact(db, old, snapshot=frozen)
    rows = hits(corpus)
    assert rows and all(row[-1]['artifact_id'] == old.id for row in rows.values())


def test_tiny_input_batches_keep_hashes_sources_and_evidence_identical(corpus, monkeypatch):
    add_documents(corpus, 2)
    db, _, first = corpus[:3]
    sources = list(db.scalars(select(Source).where(Source.domain_id == first.domain_id).order_by(Source.id)))
    make_topic(corpus, sources[:2], name='左'); make_topic(corpus, sources[1:], name='右')
    build(corpus)
    expected = hits(corpus)
    db.rollback()
    monkeypatch.setattr(snapshots, 'INPUT_BATCH_SIZE', 1)
    actual = hits(corpus)
    assert set(actual) == set(expected)
    assert {identifier: row[1].id for identifier, row in actual.items()} == {identifier: row[1].id for identifier, row in expected.items()}
    context, _, _ = inputs(corpus)
    direct = freeze_domain_inputs(db, first.owner_id, first.domain_id, scope='app')
    assert context.inputs[(first.domain_id, 'app')].model_dump() == direct.model_dump()
