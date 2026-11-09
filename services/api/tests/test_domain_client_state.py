"""Resume client progress from safe metadata without enqueueing model work."""
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.domain_schedule import schedule_view
from app.errors import ApiError
from app.models import Domain, DomainBuild, DomainSummaryJob, DomainTopic, DomainTopicMember, utcnow
from test_domain_builds import corpus, enqueue, finish
from test_domain_summary_jobs import prepared
from test_ai_analysis import storage
from test_summary_retrieval import analyzed
from services.worker import domain_build as coordinator


def state(data, scope='app'):
    return schedule_view(data[0], data[2].owner_id, data[2].domain_id, scope)


def test_no_build_has_null_progress(corpus):
    assert state(corpus)['latest_build'] is None


def test_pending_build_can_be_resumed_without_creating_paid_work(corpus):
    db = corpus[0]
    build, _ = enqueue(corpus)
    before = (db.scalar(select(func.count()).select_from(DomainBuild)),
              db.scalar(select(func.count()).select_from(DomainSummaryJob)))
    for _ in range(2):
        latest = state(corpus)['latest_build']
        assert latest['id'] == build.id and latest['status'] == 'pending'
        assert latest['topic_id'] is None
        assert all(step['status'] == 'waiting' for step in latest['steps'])
        assert 'gateway_url' not in latest and 'plan_json' not in latest
        assert 'synthetic-secret' not in json.dumps(latest)
    assert before == (db.scalar(select(func.count()).select_from(DomainBuild)),
                      db.scalar(select(func.count()).select_from(DomainSummaryJob)))


def test_running_and_completed_progress_is_restored(corpus):
    build, _ = enqueue(corpus)
    assert coordinator.run_build_once(corpus[0])
    latest = state(corpus)['latest_build']
    assert latest['status'] == 'running'
    assert any(step['status'] == 'pending' for step in latest['steps'])
    finish(corpus, [])
    latest = state(corpus)['latest_build']
    assert latest['id'] == build.id and latest['status'] == 'completed'
    assert all(step['status'] in {'completed', 'reused'} for step in latest['steps'])
    assert all(step['artifact_id'] for step in latest['steps'])


def test_latest_metadata_is_isolated_by_owner_domain_and_scope(corpus):
    db, _, source = corpus[:3]
    build, _ = enqueue(corpus)
    other_domain = Domain(owner_id=corpus[7].owner_id, name='Other owner domain')
    db.add(other_domain); db.flush()
    # Synthetic future rows make accidental unscoped ordering observable.
    for changes in ({'scope': 'shareable'}, {'domain_id': corpus[4].id},
                    {'owner_id': corpus[7].owner_id, 'domain_id': other_domain.id}):
        db.add(DomainBuild(id=str(uuid4()), owner_id=changes.get('owner_id', source.owner_id),
            domain_id=changes.get('domain_id', source.domain_id), scope=changes.get('scope', 'app'),
            generation=build.generation, authorization_id=build.authorization_id,
            gateway_url=build.gateway_url, plan_json=build.plan_json,
            created_at=utcnow() + timedelta(days=1)))
    db.commit()
    assert state(corpus)['latest_build']['id'] == build.id
    assert state(corpus, 'shareable')['latest_build']['id'] != build.id
    with pytest.raises(ApiError) as error:
        schedule_view(db, corpus[7].owner_id, source.domain_id, 'app')
    assert error.value.status_code == 404


def test_latest_topic_build_reports_target(corpus):
    db, _, source = corpus[:3]
    topic = DomainTopic(owner_id=source.owner_id, domain_id=source.domain_id,
                        scope='app', name='Synthetic topic')
    db.add(topic); db.flush()
    db.add(DomainTopicMember(topic_id=topic.id, source_id=source.id, manual=True))
    db.commit()
    enqueue(corpus, topic_id=topic.id)
    assert state(corpus)['latest_build']['topic_id'] == topic.id


def test_newest_build_is_selected_by_creation_time(corpus):
    db = corpus[0]
    first, _ = enqueue(corpus)
    first.status = 'cancelled'
    db.commit()
    second, _ = enqueue(corpus, authorize=False)
    assert state(corpus)['latest_build']['id'] == second.id
