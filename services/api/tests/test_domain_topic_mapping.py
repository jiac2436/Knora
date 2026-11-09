"""Complete topic branches become durable navigation and retrieval nodes."""
import json

import pytest
from sqlalchemy import delete, select

from app import domain_builds as builds, domain_node_index as node_index, domain_retrieval
from app.domain_summary_jobs import current_node_result
from app.errors import ApiError
from app.models import (DomainBuild, DomainSummaryArtifact, DomainSummaryJob, DomainTopic,
                        DomainTopicMember, Source, SourceProject)
from test_ai_analysis import storage
from test_domain_summary_jobs import prepared
from test_domain_builds import corpus, add_documents, enqueue, finish
from test_domain_retrieval import build, search, no_local_model
from test_summary_retrieval import analyzed


def make_topic(data, sources=None, *, scope='app', name='加工方法'):
    db, _, first = data[:3]
    topic = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id,
                        scope=scope, name=name, name_fixed=True)
    db.add(topic)
    db.flush()
    for source in sources if sources is not None else [first]:
        db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True))
    db.commit()
    return topic


def read_topic(data, topic):
    return current_node_result(data[0], data[2].owner_id, data[2].domain_id,
                               topic.scope, topic.id)


def mapped_artifacts(data, topic):
    rows = data[0].scalars(select(DomainSummaryArtifact).where(
        DomainSummaryArtifact.owner_id == data[2].owner_id,
        DomainSummaryArtifact.domain_id == data[2].domain_id,
        DomainSummaryArtifact.scope == topic.scope)).all()
    return [row for row in rows if json.loads(row.snapshot_json).get('published_topic_id') == topic.id]


def test_single_topic_leaf_is_mapped_without_replacing_domain_root(corpus):
    topic = make_topic(corpus)
    queued, plan = enqueue(corpus)
    assert plan['rules'] == 'bounded-tree-v2'
    branches = [step for step in plan['steps'] if step.get('published_topic_id') == topic.id]
    assert len(branches) == 1 and branches[0]['key'] != 'root'
    assert len(plan['steps']) == builds.plan_budget(plan)['calls'] == 2
    calls = []
    finish(corpus, calls)
    assert queued.status == 'completed' and len(calls) == 2
    topic_result = read_topic(corpus, topic)
    domain_result = current_node_result(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app')
    assert topic_result['available'] and domain_result['available']
    assert topic_result['id'] != domain_result['id']
    artifact = mapped_artifacts(corpus, topic)[0]
    frozen = json.loads(artifact.snapshot_json)
    assert topic_result['id'] == artifact.id
    assert artifact.node_id == frozen['request']['node_id']
    assert artifact.node_id not in (topic.id, corpus[2].domain_id)
    assert frozen['topic_manifest'] == branches[0]['topic_manifest']
    assert frozen['topic_coverage'] == branches[0]['topic_coverage']


def test_multibatch_topic_only_publishes_complete_branch_and_counts_extra_root(corpus):
    add_documents(corpus, 20)
    db, _, first = corpus[:3]
    sources = db.scalars(select(Source).where(Source.domain_id == first.domain_id)).all()
    topic = make_topic(corpus, sources)
    queued, plan = enqueue(corpus)
    leaves = [step for step in plan['steps'] if not step['children']]
    branch = next(step for step in plan['steps'] if step.get('published_topic_id') == topic.id)
    assert len(leaves) == 3 and all(not step.get('published_topic_id') for step in leaves)
    assert branch['children'] and branch['key'] != 'root'
    assert len(plan['steps']) == builds.plan_budget(plan)['calls'] == 6
    calls = []
    finish(corpus, calls)
    assert queued.status == 'completed' and len(calls) == 6
    assert len(mapped_artifacts(corpus, topic)) == 1
    result = read_topic(corpus, topic)
    assert result['available']
    assert result['coverage']['source_count'] == 21
    assert result['coverage']['processed_source_count'] == 21
    assert result['coverage']['cited_source_count'] <= 10


def test_multiple_topics_and_unclassified_keep_separate_membership(corpus):
    add_documents(corpus, 2)
    db, _, first = corpus[:3]
    sources = db.scalars(select(Source).where(Source.domain_id == first.domain_id).order_by(Source.id)).all()
    left = make_topic(corpus, sources[:1], name='方法')
    right = make_topic(corpus, sources[1:2], name='实践')
    queued, plan = enqueue(corpus)
    assert {s.get('published_topic_id') for s in plan['steps'] if s.get('published_topic_id')} == {left.id, right.id}
    calls = []
    finish(corpus, calls)
    assert queued.status == 'completed'
    for topic, member in ((left, sources[0]), (right, sources[1])):
        result = read_topic(corpus, topic)
        assert result['available'] and result['coverage']['source_count'] == 1
        frozen = json.loads(mapped_artifacts(corpus, topic)[0].snapshot_json)
        assert {entry['source_id'] for entry in frozen['topic_manifest']['sources']} == {member.id}
    assert current_node_result(db, first.owner_id, first.domain_id, 'app')['coverage']['processed_source_count'] == 3


def test_reused_leaf_topic_survives_removal_of_runtime_build_records(corpus):
    topic = make_topic(corpus)
    _, _ = enqueue(corpus)
    finish(corpus, [])
    original = read_topic(corpus, topic)
    queued, plan = enqueue(corpus)
    assert builds.plan_budget(plan)['reused_batches'] == 1
    calls = []
    finish(corpus, calls)
    assert queued.status == 'completed' and len(calls) == 1
    assert read_topic(corpus, topic)['id'] == original['id']
    corpus[0].execute(delete(DomainSummaryJob))
    corpus[0].execute(delete(DomainBuild))
    corpus[0].commit()
    assert read_topic(corpus, topic)['available']
    assert read_topic(corpus, topic)['id'] == original['id']


@pytest.mark.parametrize('change', ['add_unanalyzed', 'exclude', 'rename', 'delete_topic', 'edit_source'])
def test_mapped_topic_invalidates_when_membership_or_inputs_change(corpus, change):
    topic = make_topic(corpus)
    enqueue(corpus)
    finish(corpus, [])
    assert read_topic(corpus, topic)['available']
    db, _, source = corpus[:3]
    if change == 'add_unanalyzed':
        extra = Source(owner_id=source.owner_id, domain_id=source.domain_id,
                       title='尚未分析的新成员', kind='text', coverage='full_text')
        db.add(extra)
        db.flush()
        db.add(DomainTopicMember(topic_id=topic.id, source_id=extra.id, manual=True))
    elif change == 'exclude':
        db.get(DomainTopicMember, (topic.id, source.id)).included = False
    elif change == 'rename':
        topic.name = '改名后的主题'
    elif change == 'delete_topic':
        db.delete(topic)
    else:
        source.title = '原文输入变化'
    db.commit()
    try:
        result = read_topic(corpus, topic)
    except ApiError as error:
        assert change == 'delete_topic' and error.status_code == 404
    else:
        assert result['available'] is False


def test_unanalyzed_member_is_recorded_in_topic_coverage_and_dependencies(corpus):
    db, _, source = corpus[:3]
    extra = Source(owner_id=source.owner_id, domain_id=source.domain_id,
                   title='未加工资料', kind='text', coverage='full_text')
    db.add(extra)
    db.commit()
    topic = make_topic(corpus, [source, extra])
    enqueue(corpus)
    finish(corpus, [])
    result = read_topic(corpus, topic)
    assert result['available'] and result['coverage']['source_count'] == 2
    assert result['coverage']['processed_source_count'] == 1
    assert result['coverage']['excluded'][extra.id] == 'no_valid_summary'
    frozen = json.loads(mapped_artifacts(corpus, topic)[0].snapshot_json)
    assert {s['source_id'] for s in frozen['topic_manifest']['sources']} == {source.id, extra.id}


def test_mapped_branch_enters_published_node_index(corpus):
    topic = make_topic(corpus)
    enqueue(corpus)
    finish(corpus, [])
    branch = mapped_artifacts(corpus, topic)[0]
    identifiers = set(corpus[0].scalars(node_index.published_nodes().with_only_columns(DomainSummaryArtifact.id)))
    assert branch.id in identifiers
    assert node_index.node_text(branch)[1]


def test_rebuild_by_real_topic_id_queues_mapped_branch(corpus, monkeypatch):
    from dataclasses import replace
    from app import semantic
    from app.auth import AppPrincipal
    from app.domain_navigation import rebuild_node_index
    from app.domain_summary_jobs import NodeSelection
    from app.models import DomainNodeIndexJob
    topic = make_topic(corpus)
    enqueue(corpus)
    finish(corpus, [])
    branch = mapped_artifacts(corpus, topic)[0]
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
    result = rebuild_node_index(corpus[2].domain_id, NodeSelection(topic_id=topic.id),
                                AppPrincipal(corpus[2].owner_id), corpus[0])
    assert result == dict(queued=1, status='pending')
    assert corpus[0].get(DomainNodeIndexJob, branch.id).status == 'pending'


def test_purge_unanalyzed_member_removes_mapped_topic_and_domain_root(corpus):
    from app.domain_summary_jobs import purge_node_source
    db, _, source = corpus[:3]
    extra = Source(owner_id=source.owner_id, domain_id=source.domain_id,
                   title='未分析依赖', kind='text', coverage='full_text')
    db.add(extra)
    db.commit()
    topic = make_topic(corpus, [source, extra])
    enqueue(corpus)
    finish(corpus, [])
    assert read_topic(corpus, topic)['available']
    purge_node_source(db, extra.id)
    db.commit()
    assert not mapped_artifacts(corpus, topic)
    assert not current_node_result(db, source.owner_id, source.domain_id, 'app')['available']


def test_agent_narrow_project_uses_only_contained_shareable_topic_branch(corpus):
    add_documents(corpus, 1)
    db, _, source, _, _, project = corpus[:6]
    topic = make_topic(corpus, scope='shareable')
    db.add(SourceProject(source_id=source.id, project_id=project.id))
    db.commit()
    analyzed(corpus)
    build(corpus, scope='shareable')
    rows = search(corpus, agent=True, allowed={source.domain_id}, project_id=project.id)
    assert len(rows) == 1 and rows[0]['source_id'] == source.id
    assert rows[0]['evidence_id'] == corpus[3].id
    assert rows[0]['knowledge_node']['kind'] == 'topic'
    assert rows[0]['knowledge_node']['node_id'] == topic.id
    assert rows[0]['knowledge_node']['role'] == 'retrieval_hint'


def test_agent_never_loads_private_mapped_branch(corpus, monkeypatch):
    make_topic(corpus)
    build(corpus)
    def forbidden(*args, **kwargs):
        pytest.fail('私有主题产物被Agent读取')
    monkeypatch.setattr(domain_retrieval, 'current_node_result', forbidden)
    assert not search(corpus, agent=True, allowed={corpus[2].domain_id})
    assert not search(corpus, agent=True, allowed=set())
