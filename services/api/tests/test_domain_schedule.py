"""Automatic coalescing with explicit limits; synthetic data and gateway only."""
from datetime import timedelta
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing, set_processing_paused
from app.domain_schedule import schedule_domain_once, schedule_view
from app.models import DomainBuild, DomainProcessingState, DomainTopic, utcnow
from test_domain_builds import corpus, enqueue, finish, gateway, add_documents
from test_domain_summary_jobs import prepared
from test_ai_analysis import storage
from test_summary_retrieval import analyzed
from services.worker import domain_build as coordinator, domain_summary as sender


def enable(data, **changes):
    db, _, source = data[:3]
    policy = DomainProcessingPolicy(enabled=True, automatic_updates=True,
        automatic_max_sources=100, automatic_max_steps=20,
        max_calls=300, max_input_tokens=30000000).model_copy(update=changes)
    authorize_processing(db, source.owner_id, source.domain_id, 'app', policy, uuid4())
    db.commit()


def decision(data, now=None):
    db, _, source = data[:3]
    return schedule_view(db, source.owner_id, source.domain_id, 'app', now)['automatic_status']


def dirty(data, age=31, first_age=60):
    data[2].title += ' Changed'
    data[0].commit()
    analyzed(data, summary='更新后的知识加工方法。')
    state = data[0].get(DomainProcessingState, (data[2].domain_id, 'app'))
    state.dirty_since = utcnow() - timedelta(seconds=first_age)
    state.last_changed_at = utcnow() - timedelta(seconds=age)
    data[0].commit()
    return state


def baseline(data, **policy):
    build, _ = enqueue(data)
    finish(data, [])
    assert build.status == 'completed'
    enable(data, **policy)


def test_old_policy_is_off_and_opt_in_requires_explicit_scope():
    assert not DomainProcessingPolicy.model_validate_json('{"enabled":true}').automatic_updates
    with pytest.raises(ValidationError):
        DomainProcessingPolicy(enabled=True, automatic_updates=True)


def test_authorization_never_schedules_first_paid_build(corpus):
    enable(corpus)
    state = corpus[0].get(DomainProcessingState, (corpus[2].domain_id, 'app'))
    state.phase = 'dirty'
    state.dirty_since = state.last_changed_at = utcnow() - timedelta(seconds=40)
    corpus[0].commit()
    assert decision(corpus) == 'first_build_requires_confirmation'
    assert not schedule_domain_once(corpus[0])
    assert corpus[0].scalar(select(DomainBuild)) is None


def test_changes_are_coalesced_and_only_one_plan_is_queued(corpus):
    baseline(corpus)
    state = dirty(corpus, age=1)
    assert decision(corpus) == 'debouncing'
    assert not schedule_domain_once(corpus[0])
    state.last_changed_at = utcnow() - timedelta(seconds=31); corpus[0].commit()
    assert schedule_domain_once(corpus[0])
    assert not schedule_domain_once(corpus[0])
    automatic = corpus[0].scalars(select(DomainBuild).where(DomainBuild.status == 'pending')).one()
    assert json.loads(automatic.plan_json)['automatic'] is True
    calls = []; finish(corpus, calls)
    assert automatic.status == 'completed' and len(calls) == 2
    assert state.used_calls == 2 and state.dirty_since is None
    assert decision(corpus) == 'no_changes'


def test_model_switch_stops_new_automatic_work_without_failing_worker(corpus, tmp_path, monkeypatch):
    from app import model_switch
    from app.config import DEFAULT_DATABASE_URL
    baseline(corpus); dirty(corpus)
    monkeypatch.setattr(model_switch, 'settings', replace(model_switch.settings, environment='local',
        database_url=DEFAULT_DATABASE_URL, upload_dir=tmp_path / 'data/assets',
        local_data_dir=str(tmp_path / 'data'), local_cache_dir=str(tmp_path / 'cache'),
        local_log_dir=str(tmp_path / 'logs')))
    model_switch.prepare_switch(corpus[6], corpus[0])
    assert not schedule_domain_once(corpus[0])
    assert corpus[0].scalar(select(DomainBuild).where(DomainBuild.status == 'pending')) is None
    assert model_switch.configuration_state(corpus[6], corpus[0])['can_apply']


def test_same_gateway_new_profile_requires_automatic_work_confirmation(corpus, monkeypatch):
    from app import domain_builds
    baseline(corpus); dirty(corpus)
    monkeypatch.setattr(domain_builds, 'settings', replace(domain_builds.settings,
        ai_profile=dict(profile_id=str(uuid4()), config_revision=2)))
    assert decision(corpus) == 'configuration_requires_confirmation'
    assert not schedule_domain_once(corpus[0])


def test_continuous_edits_hit_max_wait_without_per_step_debounce(corpus):
    baseline(corpus)
    dirty(corpus, age=1, first_age=301)
    assert schedule_domain_once(corpus[0])
    calls = []; finish(corpus, calls)
    assert len(calls) == 2


def test_pause_preserves_unsent_checkpoint_and_budget(corpus):
    baseline(corpus)
    state = dirty(corpus)
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', True)
    corpus[0].commit()
    assert decision(corpus) == 'paused'
    assert not schedule_domain_once(corpus[0])
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', False)
    corpus[0].commit()
    assert schedule_domain_once(corpus[0])
    assert coordinator.run_build_once(corpus[0])
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', True)
    corpus[0].commit()
    calls = []
    assert not sender.run_node_once(corpus[0], gateway(calls))
    assert not coordinator.run_build_once(corpus[0])
    assert not calls and state.used_calls == 0
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', False)
    corpus[0].commit()
    finish(corpus, calls)
    assert len(calls) == 2 and state.used_calls == 2


def test_pause_during_http_keeps_response_and_stops_next_send(corpus):
    baseline(corpus); state = dirty(corpus)
    assert schedule_domain_once(corpus[0])
    assert coordinator.run_build_once(corpus[0])
    def pause(_):
        set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', True)
        corpus[0].commit()
    calls = []
    assert sender.run_node_once(corpus[0], gateway(calls, pause))
    assert state.used_calls == 1
    assert not coordinator.run_build_once(corpus[0])
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', False)
    corpus[0].commit()
    finish(corpus, calls)
    assert len(calls) == 2


@pytest.mark.parametrize('status,reason', [(504, 'unknown_requires_confirmation'), (401, 'failed_requires_manual_retry')])
def test_model_failure_never_automatically_retries(corpus, status, reason):
    baseline(corpus); dirty(corpus)
    assert schedule_domain_once(corpus[0])
    assert coordinator.run_build_once(corpus[0])
    assert sender.run_node_once(corpus[0], httpx.MockTransport(lambda _: httpx.Response(status)))
    assert coordinator.run_build_once(corpus[0])
    assert decision(corpus) == reason
    assert not schedule_domain_once(corpus[0])
    # More edits cannot erase the unresolved paid outcome.
    dirty(corpus)
    assert decision(corpus) == reason
    assert not schedule_domain_once(corpus[0])


def test_whole_plan_budget_is_cumulative_and_not_renewed(corpus):
    baseline(corpus, max_calls=2)
    state = dirty(corpus)
    assert schedule_domain_once(corpus[0])
    finish(corpus, [])
    assert state.used_calls == 2
    dirty(corpus)
    assert decision(corpus) == 'budget_exhausted'
    assert not schedule_domain_once(corpus[0])
    assert state.used_calls == 2


def test_new_content_exceeding_scope_bound_requires_manual_confirmation(corpus):
    baseline(corpus, automatic_max_sources=1)
    add_documents(corpus, 1)
    state = corpus[0].get(DomainProcessingState, (corpus[2].domain_id, 'app'))
    state.dirty_since = state.last_changed_at = utcnow() - timedelta(seconds=31)
    corpus[0].commit()
    assert decision(corpus) == 'scope_requires_confirmation'
    assert not schedule_domain_once(corpus[0])
    assert state.used_calls == 0


def test_manual_topic_structure_change_requires_preview(corpus):
    baseline(corpus)
    db, _, source = corpus[:3]
    db.add(DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id, scope='app',
        name='人工主题', name_fixed=True))
    db.commit()
    dirty(corpus)
    assert decision(corpus) == 'structure_requires_confirmation'
    assert not schedule_domain_once(db)


def test_manual_build_can_run_while_automatic_updates_are_paused(corpus):
    baseline(corpus); dirty(corpus)
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', True)
    corpus[0].commit()
    build, _ = enqueue(corpus, authorize=False)
    calls = []; finish(corpus, calls)
    assert build.status == 'completed' and len(calls) == 2


def test_state_api_is_owner_only_and_reports_safe_budget(corpus):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.auth import require_app_principal
    baseline(corpus); dirty(corpus)
    app.dependency_overrides[get_db] = lambda: corpus[0]
    app.dependency_overrides[require_app_principal] = lambda: corpus[6]
    try:
        with TestClient(app) as client:
            path = f'/v1/domains/{corpus[2].domain_id}/analysis/schedule-state'
            response = client.post(path, json={})
            assert response.status_code == 200, response.text
            assert response.json()['automatic_status'] == 'ready'
            assert response.json()['remaining_calls'] == 300
            assert 'synthetic-secret' not in response.text
            assert client.post(path, json=dict(topic_id='not-a-domain')).status_code == 422
            app.dependency_overrides[require_app_principal] = lambda: corpus[7]
            assert client.post(path, json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_manual_update_replaces_paused_automatic_pending_plan(corpus):
    baseline(corpus); state = dirty(corpus)
    assert schedule_domain_once(corpus[0])
    assert coordinator.run_build_once(corpus[0])
    old = corpus[0].scalar(select(DomainBuild).where(DomainBuild.status == 'running'))
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', True)
    corpus[0].commit()
    manual, _ = enqueue(corpus, authorize=False)
    assert old.status == 'cancelled'
    calls = []; finish(corpus, calls)
    assert manual.status == 'completed' and len(calls) == 2
    assert state.used_calls == 2


def test_paused_domain_does_not_block_manual_work_in_other_scope(corpus):
    baseline(corpus); dirty(corpus)
    assert schedule_domain_once(corpus[0])
    assert coordinator.run_build_once(corpus[0])
    set_processing_paused(corpus[0], corpus[2].owner_id, corpus[2].domain_id, 'app', True)
    corpus[0].commit()
    from test_domain_summary_jobs import queue
    shared = queue(corpus, scope='shareable')
    calls = []
    assert sender.run_node_once(corpus[0], gateway(calls))
    assert shared.status == 'completed' and len(calls) == 1


def test_changed_generation_invalidates_automatic_plan_without_charge(corpus):
    baseline(corpus); state = dirty(corpus)
    assert schedule_domain_once(corpus[0])
    assert coordinator.run_build_once(corpus[0])
    corpus[2].title += ' Another change'; corpus[0].commit()
    calls = []
    assert sender.run_node_once(corpus[0], gateway(calls))
    assert not calls and state.used_calls == 0
    assert coordinator.run_build_once(corpus[0])
    assert decision(corpus) == 'debouncing'


def test_legacy_authorization_remains_idempotent_with_new_optional_defaults(corpus):
    from app.models import DomainProcessingPreference
    from app.domain_summary_jobs import encode
    identifier = uuid4()
    db, _, source = corpus[:3]
    policy = DomainProcessingPolicy(enabled=True, max_calls=10, max_input_tokens=300000)
    authorize_processing(db, source.owner_id, source.domain_id, 'app', policy, identifier)
    preference = db.get(DomainProcessingPreference, (source.domain_id, 'app'))
    old = policy.model_dump()
    for key in ('automatic_updates', 'automatic_max_sources', 'automatic_max_steps'):
        old.pop(key)
    preference.policy_json = encode(old); db.commit()
    authorize_processing(db, source.owner_id, source.domain_id, 'app', policy, identifier)
    db.commit()


def test_new_analyzed_source_within_explicit_bounds_is_updated(corpus):
    baseline(corpus)
    add_documents(corpus, 1)
    state = corpus[0].get(DomainProcessingState, (corpus[2].domain_id, 'app'))
    state.dirty_since = state.last_changed_at = utcnow() - timedelta(seconds=31)
    corpus[0].commit()
    assert schedule_domain_once(corpus[0])
    calls = []; finish(corpus, calls)
    assert len(calls) == 2
    plan = corpus[0].scalar(select(DomainBuild).where(DomainBuild.status == 'completed')
        .order_by(DomainBuild.completed_at.desc()))
    assert json.loads(plan.plan_json)['coverage']['processed_source_count'] == 2


def test_step_bound_and_missing_summary_do_not_spend_budget(corpus):
    baseline(corpus, automatic_max_steps=1)
    state = dirty(corpus)
    assert decision(corpus) == 'scope_requires_confirmation'
    assert not schedule_domain_once(corpus[0])
    enable(corpus)
    corpus[2].current_revision_id = None
    corpus[0].commit()
    state.dirty_since = state.last_changed_at = utcnow() - timedelta(seconds=31)
    corpus[0].commit()
    assert decision(corpus) == 'no_eligible_summary'
    assert not schedule_domain_once(corpus[0])
    assert state.used_calls == 0


def test_two_scheduler_sessions_cannot_duplicate_a_generation(corpus):
    from concurrent.futures import ThreadPoolExecutor
    baseline(corpus); dirty(corpus)
    def schedule(_):
        with corpus[1]() as db:
            return schedule_domain_once(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(schedule, range(2)))
    assert sorted(results) == [False, True]
    corpus[0].expire_all()
    assert len(corpus[0].scalars(select(DomainBuild).where(DomainBuild.status == 'pending')).all()) == 1
