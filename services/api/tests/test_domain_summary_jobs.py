"""Durable node execution with synthetic gateway; no paid calls or user data."""
from dataclasses import replace
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select, update
from fastapi.testclient import TestClient

from app import domain_summary_jobs as jobs
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing, interrupt_domain_processing
from app.domain_summary_inputs import build_node_request
from app.errors import ApiError
from app.local_changes import install_change_triggers
from app.models import DomainProcessingState, DomainSummaryJob, DomainSummaryArtifact, DomainTopic, DomainTopicMember
from services.worker import domain_summary as worker
from test_ai_analysis import storage
from test_summary_retrieval import analyzed


@pytest.fixture
def prepared(storage, monkeypatch):
    analyzed(storage)
    configured = replace(jobs.settings, ai_gateway_url='http://127.0.0.1:8765', ai_gateway_token='synthetic-secret')
    monkeypatch.setattr(jobs, 'settings', configured)
    monkeypatch.setattr(worker, 'settings', configured)
    with storage[1].kw['bind'].begin() as connection:
        install_change_triggers(connection)
    return storage


def queue(data, scope='app', topic_id=None, calls=3, confirmed_unknown=False, authorize=True):
    db, _, source, _, _, _, principal, _ = data
    if authorize:
        authorize_processing(db, principal.owner_id, source.domain_id, scope,
            DomainProcessingPolicy(enabled=True, max_calls=calls, max_input_tokens=200000), uuid4())
    identifier = uuid4()
    frozen = build_node_request(db, principal.owner_id, source.domain_id, scope, identifier, topic_id)
    job = jobs.enqueue_node(db, principal.owner_id, source.domain_id, scope, identifier,
        jobs.preview_hash(frozen), topic_id, confirmed_unknown)
    db.commit()
    return job


def transport(callback=None):
    def answer(request):
        body = json.loads(request.content)
        if callback:
            callback(body)
        return httpx.Response(200, json=dict(request_id=body['request_id'], result=dict(
            contract_version=1, overview=dict(text='合成资料介绍检索方法。', citations=[body['evidence'][0]['id']]),
            insights=[], limitations=[]), provider='synthetic', model='test', prompt_version='test',
            usage=dict(prompt_tokens=10, completion_tokens=5)))
    return httpx.MockTransport(answer)


def current(data, scope='app', topic_id=None):
    return jobs.current_node_result(data[0], data[6].owner_id, data[2].domain_id, scope, topic_id)


def test_reserved_before_send_and_atomic_publication(prepared):
    db, sessions, source, *rest = prepared
    job = queue(prepared)
    def check(body):
        with sessions() as independent:
            assert independent.get(DomainSummaryJob, job.id).status == 'running'
            state = independent.get(DomainProcessingState, (source.domain_id, 'app'))
            assert state.used_calls == 1 and state.used_input_tokens == job.input_tokens
            assert state.request_id == body['request_id']
            assert independent.scalar(select(DomainSummaryArtifact)) is None
    assert worker.run_node_once(db, transport(check))
    assert job.status == 'completed'
    assert current(prepared)['available']
    assert current(prepared)['coverage']['original_text_complete'] is False
    assert db.get(DomainProcessingState, (source.domain_id, 'app')).phase == 'partial'
    assert worker.run_node_once(db, transport()) is False


@pytest.mark.parametrize('stage', ['before', 'during', 'after'])
def test_input_change_never_serves_old_overview(prepared, stage):
    db, _, source = prepared[:3]
    job = queue(prepared)
    called = []
    def change(_=None):
        source.title = 'Edited'
        db.commit()
    if stage == 'before': change()
    assert worker.run_node_once(db, transport(lambda body: (called.append(body), change() if stage == 'during' else None)))
    if stage == 'before':
        assert not called and job.status == 'stale'
        assert db.get(DomainProcessingState, (source.domain_id, 'app')).used_calls == 0
    elif stage == 'during':
        assert job.status == 'stale' and db.scalar(select(DomainSummaryArtifact)) is None
        assert db.get(DomainProcessingState, (source.domain_id, 'app')).used_calls == 1
    else:
        assert job.status == 'completed'
        change()
    assert not current(prepared)['available']


def test_unknown_no_replay_same_id_or_after_restart(prepared):
    db = prepared[0]
    job = queue(prepared)
    assert worker.run_node_once(db, httpx.MockTransport(lambda _: httpx.Response(504)))
    assert job.status == 'unknown'
    assert not worker.run_node_once(db, transport())
    frozen = json.loads(job.snapshot_json)
    again = jobs.enqueue_node(db, job.owner_id, job.domain_id, job.scope, UUID(job.id),
                              jobs.preview_hash(frozen))
    assert again.id == job.id
    with pytest.raises(ApiError) as error:
        queue(prepared, authorize=False)
    assert error.value.code == 'unknown_requires_confirmation'
    new = queue(prepared, authorize=False, confirmed_unknown=True)
    assert worker.run_node_once(db, transport()) and new.status == 'completed'
    assert db.get(DomainProcessingState, (job.domain_id, 'app')).used_calls == 2


def test_committed_claim_interruption_retains_budget(prepared):
    db = prepared[0]
    job = queue(prepared)
    def stop(_):
        raise RuntimeError('synthetic crash')
    with pytest.raises(RuntimeError):
        worker.run_node_once(db, transport(stop))
    assert job.status == 'running'
    interrupt_domain_processing(db); worker.interrupt_node_jobs(db); db.commit()
    db.refresh(job)
    assert job.status == 'unknown'
    assert db.get(DomainProcessingState, (job.domain_id, 'app')).used_calls == 1
    assert not worker.run_node_once(db, transport())


def test_old_preview_budget_and_pending_duplicate_are_blocked(prepared):
    db, _, source = prepared[:3]
    queue(prepared, calls=1)
    with pytest.raises(ApiError) as error:
        queue(prepared, authorize=False)
    assert error.value.code == 'running'
    assert worker.run_node_once(db, httpx.MockTransport(lambda _: httpx.Response(401)))
    with pytest.raises(ApiError) as error:
        queue(prepared, authorize=False)
    assert error.value.code == 'budget_exhausted'
    assert db.scalar(select(DomainSummaryArtifact)) is None


def test_changed_authorization_or_gateway_never_sends_pending_job(prepared, monkeypatch):
    db = prepared[0]
    job = queue(prepared)
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, ai_gateway_url='http://127.0.0.1:9999'))
    assert worker.run_node_once(db, transport(lambda _: pytest.fail('must not send')))
    assert job.status == 'cancelled'
    assert db.get(DomainProcessingState, (job.domain_id, 'app')).used_calls == 0


def test_scope_owner_and_topic_results_are_separate(prepared):
    db, _, source = prepared[:3]
    topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, name='Manual', scope='app', name_fixed=True)
    db.add(topic); db.flush()
    db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True)); db.commit()
    job = queue(prepared, topic_id=topic.id)
    assert worker.run_node_once(db, transport()) and job.status == 'completed'
    assert current(prepared, topic_id=topic.id)['available']
    assert not current(prepared)['available'] and not current(prepared, scope='shareable')['available']
    with pytest.raises(ApiError):
        jobs.current_node_result(db, prepared[7].owner_id, source.domain_id, 'app', topic.id)
    db.execute(delete(DomainTopicMember).where(DomainTopicMember.topic_id == topic.id)); db.commit()
    assert not current(prepared, topic_id=topic.id)['available']


def test_owner_api_json_preview_authorize_enqueue_and_status(prepared):
    from app.main import app
    from app.database import get_db
    from app.auth import require_app_principal
    db, _, source = prepared[:3]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_app_principal] = lambda: prepared[6]
    try:
        with TestClient(app) as http:
            base = f'/v1/domains/{source.domain_id}/analysis'
            preview = http.post(base + '/preview', json={})
            assert preview.status_code == 200, preview.text
            policy = dict(enabled=True, max_calls=1, max_input_tokens=200000)
            authorized = http.put(base + '/authorization', json=dict(authorization_id=str(uuid4()), policy=policy))
            assert authorized.status_code == 200, authorized.text
            payload = dict(request_id=str(uuid4()), preview_hash=preview.json()['preview_hash'])
            created = http.post(base + '/jobs', json=payload)
            assert created.status_code == 200, created.text
            assert http.post(base + '/jobs', json=payload).json()['id'] == created.json()['id']
            assert http.get(base + '/jobs/' + payload['request_id']).json()['status'] == 'pending'
            assert http.post(base + '/result', json={}).json()['available'] is False
    finally:
        app.dependency_overrides.clear()


def test_two_workers_only_one_sends_and_does_not_hold_http_write_lock(prepared):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    db, sessions, source = prepared[:3]
    job = queue(prepared)
    entered, release = Event(), Event()
    calls = []
    def send(body):
        calls.append(body['request_id'])
        entered.set()
        assert release.wait(5)
    def first():
        with sessions() as session:
            return worker.run_node_once(session, transport(send))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert entered.wait(5)
        try:
            with sessions() as independent:
                assert worker.run_node_once(independent, transport(lambda _: pytest.fail('duplicate'))) is False
                # An independent short write succeeds while HTTP is waiting.
                independent.execute(update(DomainSummaryJob).where(DomainSummaryJob.id == job.id)
                                    .values(last_error='synthetic heartbeat'))
                independent.commit()
        finally:
            release.set()
        assert future.result(timeout=5)
    db.expire_all()
    assert calls == [job.id] and db.get(DomainSummaryJob, job.id).status == 'completed'
    assert db.get(DomainProcessingState, (source.domain_id, 'app')).used_calls == 1


def test_permanent_purge_removes_node_original_excerpts(prepared):
    db, _, source = prepared[:3]
    queue(prepared)
    assert worker.run_node_once(db, transport())
    assert db.scalar(select(DomainSummaryArtifact)) is not None
    jobs.purge_node_source(db, source.id); db.commit()
    assert db.scalar(select(DomainSummaryArtifact)) is None
    assert db.scalar(select(DomainSummaryJob)) is None


def test_changed_preview_and_disabled_policy_block_queue(prepared):
    db, _, source, _, _, _, principal, _ = prepared
    identifier = uuid4()
    frozen = build_node_request(db, principal.owner_id, source.domain_id, 'app', identifier)
    with pytest.raises(ApiError) as error:
        jobs.enqueue_node(db, principal.owner_id, source.domain_id, 'app', identifier, jobs.preview_hash(frozen))
    assert error.value.code == 'disabled'
    authorize_processing(db, principal.owner_id, source.domain_id, 'app',
        DomainProcessingPolicy(enabled=True, max_calls=1, max_input_tokens=200000), uuid4())
    source.title = 'Changed after preview'
    db.commit()
    with pytest.raises(ApiError):
        jobs.enqueue_node(db, principal.owner_id, source.domain_id, 'app', identifier, jobs.preview_hash(frozen))
    assert db.scalar(select(DomainSummaryJob)) is None


def test_revoked_shareable_scope_hides_already_published_result(prepared):
    db, _, source = prepared[:3]
    queue(prepared, scope='shareable')
    assert worker.run_node_once(db, transport())
    assert current(prepared, scope='shareable')['available']
    source.visibility = 'app_only'; db.commit()
    assert current(prepared, scope='shareable') == dict(available=False, reason='stale_inputs')
