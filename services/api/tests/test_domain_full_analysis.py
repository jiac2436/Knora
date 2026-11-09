"""Whole-domain consent, deterministic checkpoints and isolated read contracts."""
from dataclasses import replace
import json
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import event, func, select

from app import ai, domain_full_analysis as full
from app.auth import require_app_principal
from app.database import get_db
from app.errors import ApiError, api_error_handler
from app.models import (AIJob, DomainBuild, DomainFullAnalysisJob, DomainProcessingPreference, DomainProcessingState,
                        DomainTopic, DomainTopicMember, EvidenceChunk, KnowledgeTag,
                        PersonalContext, Source, SourceRevision, TagAssignment)
from test_ai_analysis import storage
from test_summary_retrieval import analyzed


@pytest.fixture
def corpus(storage, monkeypatch):
    monkeypatch.setattr(full, 'settings', replace(full.settings,
        ai_gateway_url='http://127.0.0.1:8765', ai_gateway_token='synthetic-secret'))
    return storage


def plan(data, reanalyze=False):
    return full.preview_full(data[0], data[6].owner_id, data[2].domain_id, reanalyze)


def create(data, preview=None, identity=None, confirm=False, reanalyze=False):
    preview = preview or plan(data, reanalyze)
    request = full.FullEnqueue(request_id=identity or uuid4(), preview_hash=preview['preview_hash'],
        confirmed=True, confirmed_unknown=confirm, reanalyze=reanalyze)
    job = full.enqueue_full(data[0], data[6].owner_id, data[2].domain_id, request)
    data[0].commit()
    return job


def extra_document(data, *, text='第二篇合成原文', status='usable'):
    db, source = data[0], data[2]
    other = Source(owner_id=source.owner_id, domain_id=source.domain_id,
        title='另一篇合成资料', kind='text', coverage='full_text', processing_status=status)
    db.add(other); db.flush()
    revision = SourceRevision(source_id=other.id, version=1, extracted_text=text)
    db.add(revision); db.flush(); other.current_revision_id = revision.id
    db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=text, locator_end=len(text)))
    db.commit()
    return other


def test_preview_is_read_only_and_authorizes_bounded_future(corpus):
    db = corpus[0]
    before = db.scalar(select(func.count()).select_from(AIJob))
    first, second = plan(corpus), plan(corpus)
    assert first['preview_hash'] == second['preview_hash']
    assert first['total_sources'] == first['analyze_sources'] == 1
    assert first['reused_sources'] == 0 and not first['blocked_sources']
    assert first['documents'] == [dict(source_id=corpus[2].id, title=corpus[2].title, status='analyze')]
    assert first['budget_basis'] == 'bounded_upper_limit'
    assert first['calls'] == 2
    from app.domain_contract import SUMMARY_INPUT_BUDGET
    assert first['input_token_reservation'] == full.DOCUMENT_CALL_RESERVATION + SUMMARY_INPUT_BUDGET
    assert db.scalar(select(func.count()).select_from(AIJob)) == before
    assert db.scalar(select(DomainFullAnalysisJob)) is None
    assert db.scalar(select(DomainProcessingPreference)) is None
    assert 'synthetic-secret' not in full.encode(first)


def test_reuse_valid_artifact_uses_exact_overview_plan(corpus):
    artifact = analyzed(corpus, summary='有效知识概述。')
    preview = plan(corpus)
    assert preview['reused_sources'] == 1 and preview['analyze_sources'] == 0
    assert preview['budget_basis'] == 'current_exact_plan'
    assert preview['_snapshot']['documents'][0]['artifact_id'] == artifact.id
    assert preview['calls'] == 1


def test_stale_summary_is_skipped_by_default_and_counted(corpus):
    analyzed(corpus)
    corpus[2].title = '已修改'
    corpus[0].commit()
    preview = plan(corpus)
    assert preview['analyze_sources'] == preview['reused_sources'] == 0
    assert preview['stale_sources'] == preview['skipped_sources'] == 1
    assert preview['documents'][0]['status'] == 'skipped'
    assert preview['calls'] == 0
    assert preview['blocked_sources'][0]['code'] == 'full_no_eligible'
    assert plan(corpus, True)['analyze_sources'] == 1
    assert plan(corpus, True)['skipped_sources'] == 0


def test_missing_and_not_ready_sources_block_entire_enqueue(corpus):
    extra_document(corpus, status='processing')
    preview = plan(corpus)
    assert preview['total_sources'] == 2
    assert len(preview['blocked_sources']) == 1
    assert preview['blocked_sources'][0]['reason'] == '请等待资料处理完成后再分析'
    with pytest.raises(ApiError) as error:
        create(corpus, preview)
    assert error.value.code == 'full_blocked'
    assert corpus[0].scalar(select(AIJob)) is None


def test_long_document_previews_all_segment_calls_and_blocks_truncation(corpus):
    source = extra_document(corpus, text='长文证据。' * 11000)
    preview = plan(corpus)
    document = next(d for d in preview['_snapshot']['documents'] if d['source_id'] == source.id)
    assert document['calls'] > 1
    assert document['input_token_reservation'] == document['calls'] * full.DOCUMENT_CALL_RESERVATION
    db = corpus[0]
    revision = db.get(SourceRevision, source.current_revision_id)
    evidence = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id))
    revision.extracted_text = evidence.text = '长' * 250000
    db.commit()
    blocked = plan(corpus)['blocked_sources']
    assert any(item['code'] == 'full_document_too_large' for item in blocked)
    assert db.scalar(select(AIJob)) is None


@pytest.mark.parametrize('change', ['title', 'notes', 'membership', 'gateway'])
def test_preview_change_rejected_before_any_work(corpus, monkeypatch, change):
    preview = plan(corpus)
    db, source = corpus[0], corpus[2]
    if change == 'title':
        source.title = '新标题'
    elif change == 'notes':
        db.add(PersonalContext(source_id=source.id, notes='新备注'))
    elif change == 'membership':
        extra_document(corpus)
    else:
        monkeypatch.setattr(full, 'settings', replace(full.settings, ai_gateway_url='http://127.0.0.1:8766'))
    db.commit()
    with pytest.raises(ApiError) as error:
        create(corpus, preview)
    assert error.value.code == 'full_stale_preview'
    assert db.scalar(select(AIJob)) is None
    assert db.scalar(select(DomainFullAnalysisJob)) is None


def test_enqueue_idempotent_after_lost_response_without_children(corpus):
    preview, identity = plan(corpus), uuid4()
    first = create(corpus, preview, identity)
    corpus[2].title = '入队后修改'
    corpus[0].commit()
    second = create(corpus, preview, identity)
    assert first.id == second.id == str(identity)
    assert full.full_job_view(corpus[0], first)['budget']['actual_input_tokens'] is None
    assert corpus[0].scalar(select(func.count()).select_from(DomainFullAnalysisJob)) == 1
    assert corpus[0].scalar(select(AIJob)) is None
    assert corpus[0].scalar(select(DomainBuild)) is None
    changed = full.FullEnqueue(request_id=identity, preview_hash='a' * 64, confirmed=True)
    with pytest.raises(ApiError) as error:
        full.enqueue_full(corpus[0], corpus[6].owner_id, corpus[2].domain_id, changed)
    assert error.value.code == 'full_request_conflict'


def test_only_one_active_full_task_per_domain(corpus):
    create(corpus)
    with pytest.raises(ApiError) as error:
        create(corpus)
    assert error.value.code == 'full_running'


def test_document_checkpoint_is_single_and_reservation_not_repeated(corpus):
    job = create(corpus)
    child = full.enqueue_document_checkpoint(corpus[0], job, corpus[2].id)
    corpus[0].commit()
    before = job.budget_json
    assert full.enqueue_document_checkpoint(corpus[0], job, corpus[2].id).id == child.id
    assert before == job.budget_json
    assert json.loads(child.snapshot_json)['full_job_id'] == job.id
    assert json.loads(child.snapshot_json)['contract_version'] == 3
    assert json.loads(job.budget_json)['used_calls'] == 1
    assert corpus[0].scalar(select(func.count()).select_from(AIJob)) == 1
    assert 'synthetic-secret' not in job.snapshot_json


def test_checkpoint_refuses_budget_exhaustion_before_queue(corpus):
    job = create(corpus)
    budget = json.loads(job.budget_json)
    budget['calls'] = 0
    job.budget_json = full.encode(budget)
    with pytest.raises(ApiError) as error:
        full.enqueue_document_checkpoint(corpus[0], job, corpus[2].id)
    assert error.value.code == 'full_budget_exceeded'
    corpus[0].commit()  # A stopped coordinator may commit its own failure state.
    assert corpus[0].scalar(select(AIJob)) is None


def test_pipeline_outputs_allowed_but_manual_edits_stop_future_work(corpus):
    analyzed(corpus)
    job = create(corpus)
    db, source = corpus[0], corpus[2]
    tag = KnowledgeTag(owner_id=source.owner_id, name='AI新标签', normalized_name='ai新标签', dimension='topic')
    db.add(tag); db.flush()
    db.add(TagAssignment(source_id=source.id, tag_id=tag.id, origin='ai', revision_id=source.current_revision_id))
    topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='app',
        name='自动候选', automatic_key='tag:' + tag.id)
    db.add(topic); db.flush()
    db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id))
    db.commit()
    full.ensure_inputs_current(db, job)
    topic.name_fixed = True
    db.commit()
    with pytest.raises(ApiError) as error:
        full.ensure_inputs_current(db, job)
    assert error.value.code == 'full_inputs_changed'


def test_overview_checkpoint_preserves_existing_automatic_policy(corpus):
    analyzed(corpus, summary='有效概述。')
    db, source = corpus[0], corpus[2]
    preference = DomainProcessingPreference(domain_id=source.domain_id, scope='app', owner_id=source.owner_id,
        authorization_id=str(uuid4()), policy_json='{"enabled":false}')
    db.add(preference); db.commit()
    job = create(corpus)
    before = preference.policy_json, preference.authorization_id
    build = full.enqueue_overview_checkpoint(db, job)
    db.commit()
    assert (preference.policy_json, preference.authorization_id) == before
    assert build.authorization_id == job.id
    assert json.loads(build.plan_json)['full_job_id'] == job.id
    assert full.enqueue_overview_checkpoint(db, job).id == build.id


def test_unknown_document_requires_explicit_second_confirmation(corpus):
    snapshot = ai.freeze_snapshot(corpus[0], corpus[2], 'analyze', str(uuid4()), 3)
    corpus[0].add(AIJob(owner_id=corpus[6].owner_id, source_id=corpus[2].id, kind='analyze',
        status='unknown', request_key=str(uuid4()), request_sha256='a' * 64, snapshot_json=full.encode(snapshot)))
    corpus[0].commit()
    preview = plan(corpus)
    assert preview['requires_unknown_confirmation']
    with pytest.raises(ApiError) as error:
        create(corpus, preview)
    assert error.value.code == 'unknown_requires_confirmation'
    assert create(corpus, preview, confirm=True).status == 'pending'


def test_api_reads_owner_scope_and_cancel_are_idempotent(corpus):
    db, _, source, _, _, _, owner, other = corpus
    app = FastAPI()
    app.include_router(full.router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_app_principal] = lambda: owner
    base = '/v1/domains/' + source.domain_id + '/analysis/full'
    with TestClient(app) as client:
        assert client.get(base + '/latest').json() == {'job': None}
        preview = client.post(base + '/preview', json={}).json()
        assert '_snapshot' not in preview
        assert client.post(base + '/preview', json={'scope': 'shareable'}).status_code == 422
        body = dict(request_id=str(uuid4()), preview_hash=preview['preview_hash'], confirmed=True)
        response = client.post(base + '/jobs', json=body)
        assert response.status_code == 202
        job = response.json()
        assert job['id'] == body['request_id'] == job['request_id']
        assert client.get(base + '/latest').json()['job']['id'] == job['id']
        assert client.get(base + '/jobs/' + job['id']).json()['documents'][0]['status'] == 'waiting'
        stopped = client.post(base + '/jobs/' + job['id'] + '/cancel', json={}).json()
        assert stopped['status'] == 'cancelled'
        assert client.post(base + '/jobs/' + job['id'] + '/cancel', json={}).json() == stopped
        app.dependency_overrides[require_app_principal] = lambda: other
        assert client.get(base + '/latest').status_code == 404
        assert client.get(base + '/jobs/' + job['id']).status_code == 404


def test_cancel_running_document_preserves_unknown_and_stops_long_next_step(corpus):
    job = create(corpus)
    child = full.enqueue_document_checkpoint(corpus[0], job, corpus[2].id)
    child.status = 'running'
    progress = json.loads(job.progress_json)
    progress['documents'][0].update(job_id=child.id, status='running')
    job.progress_json = full.encode(progress)
    corpus[0].commit()
    view = full.cancel_endpoint(corpus[2].domain_id, job.id, full.FullSelection(), corpus[6], corpus[0])
    assert view['status'] == 'unknown'
    assert child.status == 'cancelled'
    assert corpus[0].get(DomainProcessingState, (job.domain_id, 'app')).phase == 'unknown'
    assert plan(corpus)['requires_unknown_confirmation']
    with pytest.raises(ApiError) as error:
        create(corpus)
    assert error.value.code == 'unknown_requires_confirmation'
    with pytest.raises(ApiError):
        full.enqueue_document_checkpoint(corpus[0], job, corpus[2].id)


def test_wait_for_independent_active_job_does_not_cancel_it(corpus):
    active = ai.create_job(ai.JobRequest(source_id=corpus[2].id, kind='analyze', contract_version=3,
        request_key=uuid4()), corpus[6], corpus[0])
    job = create(corpus)
    child = full.enqueue_document_checkpoint(corpus[0], job, corpus[2].id)
    assert child.id == active['id']
    progress = json.loads(job.progress_json)
    progress['documents'][0].update(job_id=child.id, status='pending')
    job.progress_json = full.encode(progress)
    corpus[0].commit()
    full.cancel_endpoint(corpus[2].domain_id, job.id, full.FullSelection(), corpus[6], corpus[0])
    assert child.status == 'pending'


def test_manual_tag_rename_invalidates_while_ai_only_catalog_does_not(corpus):
    db, source = corpus[0], corpus[2]
    tag = KnowledgeTag(owner_id=source.owner_id, name='人工分类', normalized_name='人工分类', dimension='topic')
    db.add(tag); db.flush()
    db.add(TagAssignment(source_id=source.id, tag_id=tag.id, origin='manual'))
    db.commit()
    job = create(corpus)
    tag.name = '人工已改名'
    db.commit()
    with pytest.raises(ApiError) as error:
        full.ensure_inputs_current(db, job)
    assert error.value.code == 'full_inputs_changed'


def test_overlapping_topics_do_not_duplicate_overview_work(corpus):
    db, source = corpus[0], corpus[2]
    # Themes organize browsing; overlapping memberships must not multiply paid work.
    for ordinal in range(130):
        topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='app',
            name=f'人工主题 {ordinal}', name_fixed=True)
        db.add(topic); db.flush()
        db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True))
    db.commit()
    preview = plan(corpus)
    assert not preview['blocked_sources']
    assert preview['calls'] == 2  # One document analysis plus one overview.
    assert create(corpus, preview).status == 'pending'
    assert db.scalar(select(AIJob)) is None


def test_full_api_requires_actual_app_authentication(corpus):
    # The router uses the same strict App dependency as AI mutation APIs;
    # absent bearer credentials cannot read even a directory preview.
    app = FastAPI()
    app.include_router(full.router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[get_db] = lambda: corpus[0]
    base = '/v1/domains/' + corpus[2].domain_id + '/analysis/full'
    with TestClient(app) as client:
        assert client.post(base + '/preview', json={}).status_code == 401
        assert client.get(base + '/latest').status_code == 401
        assert client.post(base + '/jobs/' + str(uuid4()) + '/cancel', json={}).status_code == 401


def test_scope_validation_reads_dependencies_in_batches(corpus):
    db = corpus[0]
    for _ in range(40):
        extra_document(corpus)
    owner, domain = corpus[6].owner_id, corpus[2].domain_id
    db.expire_all()
    queries = []
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        frozen = full.current_full_snapshot(db, owner, domain)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)
    assert len(frozen['sources']) == 41
    assert len(queries) <= 14  # One batch, not seven or more queries per source.
    assert all(query.lstrip().upper().startswith('SELECT') for query in queries)


@pytest.mark.parametrize('confirmation', [False, 1, 'true', None])
def test_enqueue_requires_literal_boolean_confirmation(confirmation):
    with pytest.raises(ValidationError):
        full.FullEnqueue(request_id=str(uuid4()), preview_hash='a' * 64, confirmed=confirmation)


def test_reanalysis_mode_is_bound_to_preview_and_idempotency(corpus):
    analyzed(corpus)
    normal, forced = plan(corpus), plan(corpus, True)
    assert forced['reanalyze'] and forced['reused_sources'] == 0 and forced['analyze_sources'] == 1
    assert normal['preview_hash'] != forced['preview_hash']
    with pytest.raises(ApiError) as error:
        create(corpus, normal, reanalyze=True)
    assert error.value.code == 'full_stale_preview'
    job = create(corpus, forced, reanalyze=True)
    assert create(corpus, forced, identity=job.id, reanalyze=True).id == job.id
    with pytest.raises(ApiError) as error:
        create(corpus, forced, identity=job.id, reanalyze=False)
    assert error.value.code == 'full_request_conflict'


def test_skipped_history_survives_backup_without_operational_job(corpus):
    artifact = analyzed(corpus)
    child = corpus[0].get(AIJob, artifact.job_id)
    corpus[0].delete(child)
    corpus[2].title = '修改标题'
    corpus[0].commit()
    assert plan(corpus)['skipped_sources'] == 1
