"""Persistent tasks decide configuration changes, independently of UI busy flags."""
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from app import ai, model_switch as switch
from app.config import DEFAULT_DATABASE_URL
from app.errors import ApiError
from app.models import AIJob, AIArtifact
from sqlalchemy import select
from services.worker import ai_analysis
from test_ai_analysis import storage, create, complete


@pytest.fixture
def local(storage, tmp_path, monkeypatch):
    configured = replace(switch.settings, environment='local', local_data_dir=str(tmp_path / 'data'),
        local_cache_dir=str(tmp_path / 'cache'), local_log_dir=str(tmp_path / 'logs'),
        database_url=DEFAULT_DATABASE_URL, upload_dir=tmp_path / 'data' / 'assets')
    monkeypatch.setattr(switch, 'settings', configured)
    return storage


def test_switch_waits_for_persistent_tasks_without_cancelling_them(local):
    key = uuid4()
    job = create(local, key=key)
    db, _, _, _, _, _, owner, _ = local
    prepared = switch.prepare_switch(owner, db)
    assert prepared['state'] == 'waiting' and prepared['tasks']['pending'] == 1
    assert not prepared['can_apply']
    assert switch.prepare_switch(owner, db)['switch_id'] == prepared['switch_id']
    assert create(local, key=key)['id'] == job['id']  # Re-reading an accepted request is still idempotent.
    with pytest.raises(ApiError) as blocked:
        create(local)
    assert blocked.value.code == 'model_configuration_pending'
    db.rollback()
    assert complete(local)
    assert db.get(AIJob, job['id']).status == 'completed'
    state = switch.configuration_state(owner, db)
    assert state['can_apply'] and state['tasks'] == dict(pending=0, running=0, unknown=0)
    assert switch._path().stat().st_mode & 0o777 == 0o600
    switch.clear_startup_gate()
    assert switch.configuration_state(owner, db)['state'] == 'active'


def test_sent_and_unknown_tasks_are_not_rewritten_or_replayed(local):
    job = create(local)
    db, _, _, _, _, _, owner, _ = local
    row = db.get(AIJob, job['id']); original = row.snapshot_json
    row.status = 'running'; db.commit()
    assert switch.prepare_switch(owner, db)['tasks']['running'] == 1
    assert not switch.configuration_state(owner, db)['can_apply']
    row.status = 'unknown'; db.commit()
    assert switch.configuration_state(owner, db)['can_apply']
    assert switch.configuration_state(owner, db)['tasks']['unknown'] == 1
    assert row.status == 'unknown' and row.snapshot_json == original
    assert not complete(local)


def test_cancel_switch_requires_current_identity_and_resumes_accepting(local):
    db, _, _, _, _, _, owner, _ = local
    state = switch.prepare_switch(owner, db)
    with pytest.raises(ApiError):
        switch.cancel_switch(switch.SwitchSelection(switch_id=uuid4()), owner, db)
    db.rollback()
    switch.cancel_switch(switch.SwitchSelection(switch_id=state['switch_id']), owner, db)
    assert create(local)['status'] == 'pending'


def test_gate_rejects_symlink_without_removing_target(local, tmp_path):
    target = tmp_path / 'target'; target.write_text('preserve')
    switch._path().parent.mkdir(parents=True, exist_ok=True)
    switch._path().symlink_to(target)
    with pytest.raises(ApiError):
        switch.clear_startup_gate()
    assert target.read_text() == 'preserve'


def test_legacy_or_different_revision_cannot_be_sent_to_new_configuration(local, monkeypatch):
    job = create(local)
    db = local[0]
    changed = replace(ai_analysis.settings, ai_profile={'profile_id': str(uuid4()), 'config_revision': 2})
    monkeypatch.setattr(ai_analysis, 'settings', changed)
    calls = []
    assert complete(local, lambda request: calls.append(request))
    row = db.get(AIJob, job['id'])
    assert row.status == 'failed' and '配置代次' in row.last_error
    assert not calls


def test_new_snapshot_freezes_public_binding_only(local, monkeypatch):
    binding = dict(profile_id=str(uuid4()), config_revision=7, provider='openai-compatible',
                   model='synthetic', credential_ref='synthetic-ref', credential_generation=str(uuid4()))
    monkeypatch.setattr(ai, 'settings', replace(ai.settings, ai_profile=binding))
    monkeypatch.setattr(ai_analysis, 'settings', ai.settings)
    job = create(local)
    frozen = json.loads(local[0].get(AIJob, job['id']).snapshot_json)
    assert frozen['model_binding'] == binding
    assert 'provider_key' not in frozen and 'gateway_token' not in frozen
    assert complete(local)
    artifact = local[0].scalar(select(AIArtifact))
    assert 'model_binding' not in json.loads(artifact.snapshot_json)
    assert 'gateway_url' not in json.loads(artifact.snapshot_json)
    assert 'credential_ref' not in artifact.snapshot_json
