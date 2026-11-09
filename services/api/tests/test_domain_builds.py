"""Multi-batch plans, bounded original-backed merges and durable reuse."""
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app import domain_builds as builds
from app.domain_contract import DomainSummaryRequest, DomainSummaryResult, validate_domain_summary
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing, interrupt_domain_processing
from app.domain_summary_jobs import NodeEnqueue, current_node_result
from app.errors import ApiError
from app.models import DomainBuild, DomainSummaryJob, DomainSummaryArtifact, DomainProcessingState, utcnow, Source, SourceRevision, EvidenceChunk
from services.worker import domain_build as coordinator
from services.worker import domain_summary as sender
from test_domain_summary_jobs import prepared
from test_ai_analysis import storage
from test_summary_retrieval import analyzed


@pytest.fixture
def corpus(prepared, monkeypatch):
    # Keep exercising persisted v1 trees and their topic mappings after v2 becomes default.
    monkeypatch.setattr(builds, 'prepare_plan', builds.prepare_legacy_plan)
    monkeypatch.setattr(builds, 'settings', replace(builds.settings,
        ai_gateway_url='http://127.0.0.1:8765', ai_gateway_token='synthetic-secret'))
    return prepared


def add_documents(data, number):
    db, _, first = data[:3]
    for i in range(number):
        source = Source(owner_id=first.owner_id, domain_id=first.domain_id, title=f'Synthetic {i}',
            kind='text', coverage='full_text')
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text=f'原文方法 {i}。')
        db.add(revision); db.flush()
        source.current_revision_id = revision.id
        evidence = EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text, locator_end=len(revision.extracted_text))
        db.add(evidence); db.commit()
        changed = list(data); changed[2:4] = source, evidence
        analyzed(tuple(changed), summary=f'方法 {i}。')


def enqueue(data, confirm=False, authorize=True, topic_id=None):
    db, _, source = data[:3]
    plan = builds.prepare_plan(db, source.owner_id, source.domain_id, 'app', topic_id)
    budget = builds.plan_budget(plan)
    if authorize:
        authorize_processing(db, source.owner_id, source.domain_id, 'app', DomainProcessingPolicy(
            enabled=True, max_calls=300, max_input_tokens=30000000), uuid4())
    body = NodeEnqueue(request_id=str(uuid4()), preview_hash=builds.plan_hash(plan),
        confirmed_unknown=confirm, topic_id=topic_id)
    build = builds.enqueue_build(db, source.owner_id, source.domain_id, body)
    db.commit()
    return build, plan


def gateway(calls, change=None):
    def handle(request):
        body = json.loads(request.content)
        DomainSummaryRequest.model_validate(body)
        calls.append(body)
        if change: change(body)
        # Cite two originals while keeping every merged fact grounded.
        citations = [e['id'] for e in body['evidence'][:2]]
        return httpx.Response(200, json=dict(request_id=body['request_id'], provider='synthetic', model='test', prompt_version='test',
            usage=dict(prompt_tokens=10, completion_tokens=10), result=dict(contract_version=1,
                overview=dict(text='这些资料介绍知识加工方法。', citations=citations), insights=[], limitations=[])))
    return httpx.MockTransport(handle)


def finish(data, calls, transport=None):
    for _ in range(300):
        if coordinator.run_build_once(data[0]): continue
        if sender.run_node_once(data[0], transport or gateway(calls)): continue
        return
    pytest.fail('did not converge')


def test_more_than_twenty_documents_are_processed_in_separate_batches(corpus):
    add_documents(corpus, 20)
    build, plan = enqueue(corpus)
    leaves = [s for s in plan['steps'] if not s['children']]
    assert len(leaves) == 3
    ids = [s['source_ids'][0] for leaf in leaves for s in leaf['frozen']['request']['summaries']]
    assert len(ids) == len(set(ids)) == 21
    assert plan['coverage']['processed_source_count'] == 21
    assert not plan['coverage']['excluded']
    calls = []
    finish(corpus, calls)
    assert build.status == 'completed' and len(calls) == 5
    assert len(calls[-1]['summaries']) == 2
    assert all(s['kind'] == 'topic' for s in calls[-1]['summaries'])
    db, _, source = corpus[:3]
    result = current_node_result(db, source.owner_id, source.domain_id, 'app')
    assert result['available'] and result['coverage']['processed_source_count'] == 21
    assert result['coverage']['original_text_complete'] is False
    assert db.get(DomainProcessingState, (source.domain_id, 'app')).used_calls == 5
    assert db.get(DomainProcessingState, (source.domain_id, 'app')).phase == 'partial'


def test_completed_batches_reuse_but_changed_document_is_reprocessed(corpus):
    add_documents(corpus, 10)
    first, _ = enqueue(corpus)
    calls = []; finish(corpus, calls)
    second, plan = enqueue(corpus)
    assert builds.plan_budget(plan)['reused_batches'] == 2
    next_calls = []; finish(corpus, next_calls)
    assert second.status == 'completed' and len(next_calls) == 1
    db, _, source = corpus[:3]
    source.title = 'Changed'
    db.commit()
    analyzed(corpus, summary='更新后的知识加工方法。')
    third, changed = enqueue(corpus)
    assert builds.plan_budget(changed)['reused_batches'] == 1
    changed_calls = []; finish(corpus, changed_calls)
    assert third.status == 'completed' and len(changed_calls) == 2


def test_generation_revision_stops_reuse_without_rewriting_existing_artifacts(corpus, monkeypatch):
    revision = builds.GENERATION_REVISION
    monkeypatch.setattr(builds, 'GENERATION_REVISION', 'synthetic-old-generation')
    first, _ = enqueue(corpus)
    finish(corpus, [])
    db, _, source = corpus[:3]
    before = {a.id: (a.snapshot_json, a.response_json) for a in db.scalars(select(DomainSummaryArtifact))}
    monkeypatch.setattr(builds, 'GENERATION_REVISION', revision)
    assert current_node_result(db, source.owner_id, source.domain_id, 'app')['available']
    second, plan = enqueue(corpus)
    assert plan['generation_revision'] == revision
    assert builds.plan_budget(plan)['reused_batches'] == 0
    calls = []; finish(corpus, calls)
    assert first.status == second.status == 'completed' and len(calls) == 2
    assert all((db.get(DomainSummaryArtifact, key).snapshot_json,
        db.get(DomainSummaryArtifact, key).response_json) == value for key, value in before.items())


def test_unknown_stops_plan_and_retains_completed_batch_for_explicit_retry(corpus):
    add_documents(corpus, 10)
    build, _ = enqueue(corpus)
    assert coordinator.run_build_once(corpus[0])
    calls = []
    assert sender.run_node_once(corpus[0], gateway(calls))
    assert coordinator.run_build_once(corpus[0])
    assert sender.run_node_once(corpus[0], httpx.MockTransport(lambda _: httpx.Response(504)))
    assert coordinator.run_build_once(corpus[0])
    assert build.status == 'unknown'
    assert coordinator.run_build_once(corpus[0]) is False
    with pytest.raises(ApiError) as error:
        enqueue(corpus, authorize=False)
    assert error.value.code == 'unknown_requires_confirmation'
    again, plan = enqueue(corpus, authorize=False, confirm=True)
    assert builds.plan_budget(plan)['reused_batches'] == 1
    retry_calls = []; finish(corpus, retry_calls)
    assert again.status == 'completed' and len(retry_calls) == 2


def test_scope_change_invalidates_plan_before_unsent_batch(corpus):
    build, _ = enqueue(corpus)
    assert coordinator.run_build_once(corpus[0])
    corpus[2].deleted_at = utcnow()
    corpus[0].commit()
    assert coordinator.run_build_once(corpus[0])
    assert build.status == 'stale'
    assert not sender.run_node_once(corpus[0], gateway([]))
    assert corpus[0].scalar(select(DomainSummaryArtifact)) is None


def test_tree_output_reference_budget_is_enforced_before_merge(corpus):
    plan = builds.prepare_plan(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app')
    request = DomainSummaryRequest.model_validate(plan['steps'][0]['frozen']['request'])
    request.max_cited_characters = 3000
    request.evidence[0].text = '字' * 3000
    result = DomainSummaryResult(overview=dict(text='合成。', citations=[request.evidence[0].id]))
    validate_domain_summary(request, result)
    request.max_cited_evidence = 1
    extra = request.evidence[0].model_copy(update=dict(id=str(uuid4())))
    request.evidence.append(extra)
    result.overview.citations.append(extra.id)
    with pytest.raises(ValueError): validate_domain_summary(request, result)


@pytest.mark.parametrize('child_windows', [6, 9])
def test_terminal_root_retains_bounded_union_and_intermediates_fit_next_merge(corpus, child_windows):
    from app.domain_inputs import freeze_domain_inputs
    db, _, source = corpus[:3]
    manifest = freeze_domain_inputs(db, source.owner_id, source.domain_id).model_dump()
    children = []
    for number in range(2):
        request_for_child = DomainSummaryRequest.model_validate(
            builds.prepare_plan(db, source.owner_id, source.domain_id, 'app')['steps'][0]['frozen']['request'])
        evidence = [request_for_child.evidence[0].model_copy(update=dict(id=str(uuid4()),
            text='合' * 1000, text_start=1000 * (number * child_windows + offset),
            text_end=1000 * (number * child_windows + offset + 1))) for offset in range(child_windows)]
        request_for_child.evidence = evidence
        request_for_child.max_cited_characters = 9000
        request_for_child.summaries[0].citations = [e.id for e in evidence]
        request_for_child = DomainSummaryRequest.model_validate(request_for_child.model_dump())
        result = DomainSummaryResult(overview=dict(text='合成子节点。', citations=[e.id for e in evidence]))
        validate_domain_summary(request_for_child, result)
        artifact = DomainSummaryArtifact(id=str(uuid4()), owner_id=source.owner_id,
            domain_id=source.domain_id, scope='app', node_id=request_for_child.node_id,
            snapshot_json=json.dumps(dict(request=request_for_child.model_dump(), manifest=manifest)),
            response_json=json.dumps(dict(request_id=request_for_child.request_id,
                provider='synthetic', model='test', prompt_version='test',
                usage=dict(prompt_tokens=1, completion_tokens=1), result=result.model_dump())))
        db.add(artifact)
        children.append(dict(key=f'child:{number}', reuse_id=artifact.id))
    db.commit()
    build = DomainBuild(id=str(uuid4()), owner_id=source.owner_id, domain_id=source.domain_id, scope='app')
    step = dict(key='root', children=[c['key'] for c in children], node_id=source.domain_id,
        title='合成概览', input_tokens=builds.PARENT_INPUT_RESERVATION)
    plan = dict(topic_id=None, manifest=manifest, coverage={}, steps=[*children, step])
    terminal = DomainSummaryRequest.model_validate(builds.materialize_step(db, build, plan, step)['request'])
    assert (terminal.max_cited_characters, terminal.max_cited_sources, terminal.max_cited_evidence) == (18000, 20, 80)
    assert sum(len(e.text) for e in terminal.evidence) == 2000 * child_windows
    output = DomainSummaryResult(overview=dict(text='合成概览。', citations=[e.id for e in terminal.evidence[:child_windows]]),
        insights=[dict(text='合成补充。', citations=[e.id for e in terminal.evidence[child_windows:]])])
    validate_domain_summary(terminal, output)
    step['key'] = 'merge:intermediate'
    bounded = builds.materialize_step(db, build, plan, step)
    intermediate = DomainSummaryRequest.model_validate(bounded['request'])
    assert (intermediate.max_cited_characters, intermediate.max_cited_sources, intermediate.max_cited_evidence) == (9000, 10, 40)
    assert sum(len(e.text) for e in intermediate.evidence) <= 9000
    assert not bounded['evidence_selection']['all_provided_evidence_retained']
    validate_domain_summary(intermediate, DomainSummaryResult(overview=dict(text='有界合成。',
        citations=[e.id for e in intermediate.evidence])))
    with pytest.raises(ValueError, match='citation'):
        validate_domain_summary(intermediate, output)


def test_existing_topics_merge_before_domain_and_unclassified_is_not_lost(corpus):
    add_documents(corpus, 2)
    from app.models import DomainTopic, DomainTopicMember
    db, _, first = corpus[:3]
    sources = db.scalars(select(Source).where(Source.domain_id == first.domain_id).order_by(Source.id)).all()
    topics = [DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app', name=name, name_fixed=True)
              for name in ('方法', '实践')]
    db.add_all(topics); db.flush()
    for topic, source in zip(topics, sources[:2]):
        db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True))
    db.commit()
    build, plan = enqueue(corpus)
    leaves = [s for s in plan['steps'] if not s['children']]
    assert len(leaves) == 3
    assert {s['selector'] for s in leaves} == {topics[0].id, topics[1].id, None}
    calls = []; finish(corpus, calls)
    assert build.status == 'completed' and len(calls) == 5
    assert plan['coverage']['processed_source_count'] == 3
    assert db.scalar(select(DomainSummaryArtifact).where(DomainSummaryArtifact.node_id == first.domain_id)) is not None


def test_unauthorized_filtered_and_unanalyzed_sources_remain_explicit(corpus):
    db, _, source = corpus[:3]
    unanalyzed = Source(owner_id=source.owner_id, domain_id=source.domain_id, title='Pending analysis',
        kind='text', coverage='full_text', visibility='app_only')
    db.add(unanalyzed); db.commit()
    app = builds.prepare_plan(db, source.owner_id, source.domain_id, 'app')
    assert app['coverage']['source_count'] == 2 and app['coverage']['processed_source_count'] == 1
    assert app['coverage']['excluded'] == {unanalyzed.id: 'no_valid_summary'}
    shared = builds.prepare_plan(db, source.owner_id, source.domain_id, 'shareable')
    assert shared['coverage']['source_count'] == 1
    assert unanalyzed.id not in json.dumps(shared)
    with pytest.raises(ApiError):
        builds.prepare_plan(db, corpus[7].owner_id, source.domain_id, 'app')


def test_entire_plan_budget_and_preview_conflict_checked_before_enqueue(corpus):
    db, _, source = corpus[:3]
    plan = builds.prepare_plan(db, source.owner_id, source.domain_id, 'app')
    authorize_processing(db, source.owner_id, source.domain_id, 'app',
        DomainProcessingPolicy(enabled=True, max_calls=1, max_input_tokens=300000), uuid4())
    body = NodeEnqueue(request_id=str(uuid4()), preview_hash=builds.plan_hash(plan))
    with pytest.raises(ApiError) as error:
        builds.enqueue_build(db, source.owner_id, source.domain_id, body)
    assert error.value.code == 'budget_exhausted'
    assert db.scalar(select(DomainBuild)) is None and db.scalar(select(DomainSummaryJob)) is None
    authorize_processing(db, source.owner_id, source.domain_id, 'app',
        DomainProcessingPolicy(enabled=True, max_calls=2, max_input_tokens=300000), uuid4())
    source.title = 'Different preview'; db.commit()
    analyzed(corpus, summary='更新后的合成摘要。')
    with pytest.raises(ApiError) as error:
        builds.enqueue_build(db, source.owner_id, source.domain_id, body)
    assert error.value.code == 'stale_preview'


def test_hard_stop_never_replays_claim_but_keeps_clean_completed_steps(corpus):
    build, _ = enqueue(corpus)
    assert coordinator.run_build_once(corpus[0])
    def crash(_): raise RuntimeError('synthetic interruption')
    with pytest.raises(RuntimeError):
        sender.run_node_once(corpus[0], httpx.MockTransport(crash))
    interrupt_domain_processing(corpus[0]); sender.interrupt_node_jobs(corpus[0])
    coordinator.interrupt_builds(corpus[0]); corpus[0].commit()
    assert build.status == 'unknown'
    calls = []; finish(corpus, calls)
    assert calls == []


def test_topic_build_is_scoped_and_published_separately(corpus):
    from app.models import DomainTopic, DomainTopicMember
    db, _, source = corpus[:3]
    topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='app', name='主题', name_fixed=True)
    db.add(topic); db.flush()
    db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True)); db.commit()
    build, plan = enqueue(corpus, topic_id=topic.id)
    calls = []; finish(corpus, calls)
    assert build.status == 'completed'
    assert current_node_result(db, source.owner_id, source.domain_id, 'app', topic.id)['available']
    assert current_node_result(db, source.owner_id, source.domain_id, 'app')['available'] is False
    topic.name = 'Changed'; db.commit()
    assert current_node_result(db, source.owner_id, source.domain_id, 'app', topic.id)['available'] is False


def test_build_api_preview_enqueue_and_idempotent_progress(corpus):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.auth import require_app_principal
    db, _, source = corpus[:3]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_app_principal] = lambda: corpus[6]
    try:
        with TestClient(app) as http:
            base = f'/v1/domains/{source.domain_id}/analysis'
            preview = http.post(base + '/build/preview', json={})
            assert preview.status_code == 200, preview.text
            budget = preview.json()
            assert budget['calls'] == 2 and budget['total_steps'] == 2
            policy = dict(enabled=True, max_calls=budget['calls'], max_input_tokens=budget['input_token_reservation'])
            assert http.put(base + '/authorization', json=dict(authorization_id=str(uuid4()), policy=policy)).status_code == 200
            body = dict(request_id=str(uuid4()), preview_hash=budget['preview_hash'])
            created = http.post(base + '/build/jobs', json=body)
            assert created.status_code == 200, created.text
            assert http.post(base + '/build/jobs', json=body).json()['id'] == body['request_id']
            assert http.get(base + '/build/jobs/' + body['request_id']).json()['status'] == 'pending'
            assert http.get('/v1/domains/not-owned/analysis/build/jobs/' + body['request_id']).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_duplicate_automatic_candidate_does_not_repeat_manual_branch(corpus):
    from app.models import DomainTopic, DomainTopicMember
    db, _, first = corpus[:3]
    topics = [DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app',
                         name='人工主题', name_fixed=True),
              DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app',
                         name='自动候选', automatic_key='tag:synthetic')]
    db.add_all(topics); db.flush()
    for topic in topics:
        db.add(DomainTopicMember(topic_id=topic.id, source_id=first.id))
    db.commit()
    build, plan = enqueue(corpus)
    leaves = [s for s in plan['steps'] if not s['children']]
    assert [s['selector'] for s in leaves] == [topics[0].id]
    assert builds.plan_budget(plan)['calls'] == 2
    calls = []; finish(corpus, calls)
    assert build.status == 'completed' and len(calls) == 2
    # Suppression changes this plan only; user taxonomy/membership is preserved.
    assert db.get(DomainTopicMember, (topics[1].id, first.id)).included
    assert current_node_result(db, first.owner_id, first.domain_id, 'app', topics[0].id)['available']
    explicit = builds.prepare_plan(db, first.owner_id, first.domain_id, 'app', topics[1].id)
    assert explicit['steps'][0]['selector'] == topics[1].id


@pytest.mark.parametrize('fixed,parent,extra', [(True, False, False), (False, True, False), (False, False, True)])
def test_distinct_or_human_customized_automatic_topic_is_retained(corpus, fixed, parent, extra):
    from app.models import DomainTopic, DomainTopicMember
    db, _, first = corpus[:3]
    if extra:
        add_documents(corpus, 1)
    sources = db.scalars(select(Source).where(Source.domain_id == first.domain_id)).all()
    manual = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app', name='人工', name_fixed=True)
    db.add(manual); db.flush()
    automatic = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app', name='候选',
        automatic_key='tag:synthetic', name_fixed=fixed, parent_id=manual.id if parent else None)
    db.add(automatic); db.flush()
    db.add(DomainTopicMember(topic_id=manual.id, source_id=first.id))
    for source in sources:
        db.add(DomainTopicMember(topic_id=automatic.id, source_id=source.id))
    db.commit()
    plan = builds.prepare_plan(db, first.owner_id, first.domain_id, 'app')
    assert {s['selector'] for s in plan['steps'] if not s['children']} == {manual.id, automatic.id}
    assert plan['coverage']['processed_source_count'] == len(sources)


def test_two_manual_groups_and_three_duplicate_candidates_need_three_calls(corpus):
    from app.models import DomainTopic, DomainTopicMember
    add_documents(corpus, 3)
    db, _, first = corpus[:3]
    ids = sorted(db.scalars(select(Source.id).where(Source.domain_id == first.domain_id)))
    for name, members, automatic in [
        ('人工索引', ids[:2], None), ('人工缓存', ids[2:], None),
        ('索引更新', ids[:2], 'tag:one'), ('失败处理', ids[:2], 'tag:two'),
        ('缓存策略', ids[2:], 'tag:three'),
    ]:
        topic = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app',
                            name=name, automatic_key=automatic, name_fixed=automatic is None)
        db.add(topic); db.flush()
        for identifier in members:
            db.add(DomainTopicMember(topic_id=topic.id, source_id=identifier))
    db.commit()
    build, plan = enqueue(corpus)
    assert len(plan['steps']) == builds.plan_budget(plan)['calls'] == 3
    assert plan['coverage']['processed_source_count'] == 4
    calls = []; finish(corpus, calls)
    assert build.status == 'completed' and len(calls) == 3


def test_manually_edited_candidate_membership_is_not_suppressed(corpus):
    from app.models import DomainTopic, DomainTopicMember
    db, _, first = corpus[:3]
    manual = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app', name='人工', name_fixed=True)
    candidate = DomainTopic(owner_id=first.owner_id, domain_id=first.domain_id, scope='app', name='候选', automatic_key='tag:one')
    db.add_all([manual, candidate]); db.flush()
    db.add_all([DomainTopicMember(topic_id=t.id, source_id=first.id, manual=True) for t in [manual, candidate]])
    db.commit()
    plan = builds.prepare_plan(db, first.owner_id, first.domain_id, 'app')
    assert {s['selector'] for s in plan['steps'] if not s['children']} == {manual.id, candidate.id}
