"""Domain dirty state, budget transactions and v8 compatibility, without paid calls."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app import migrations
from app.database import make_engine
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing, interrupt_domain_processing, reserve_domain_call, set_processing_paused
from app.errors import ApiError
from app.local_backup import export_backup, restore_backup
from app.local_changes import validate_change_triggers
from app.local_paths import local_paths
from app.models import Domain, DomainProcessingPreference, DomainProcessingState, Source, utcnow
from app.sync_outbox import prepare_operation
from test_local_changes import local_library, capture_note
from test_local_backup import library


def state(db, domain, scope="app"):
    db.expire_all()
    return db.get(DomainProcessingState, (domain, scope))


def test_business_write_and_dirty_state_rollback_together(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        before = state(db, identity['domain_id']).generation
        db.execute(update(Source).where(Source.id == saved['id']).values(title="rolled back"))
        assert state(db, identity['domain_id']).generation == before + 1
        db.rollback()
        assert state(db, identity['domain_id']).generation == before
        db.execute(update(Source).where(Source.id == saved['id']).values(title="committed"))
        db.commit()
    with sessions() as db:
        assert state(db, identity['domain_id']).generation == before + 1


def test_move_marks_both_domains_and_private_edit_does_not_dirty_shared(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        source = db.get(Source, saved['id'])
        source.visibility = 'app_only'; db.commit()
        shared = state(db, identity['domain_id'], 'shareable').generation
        target = Domain(owner_id=source.owner_id, name='Destination')
        db.add(target); db.commit()
        source.title = 'private edit'; db.commit()
        assert state(db, identity['domain_id'], 'shareable').generation == shared
        original = state(db, identity['domain_id']).generation
        source.domain_id = target.id; db.commit()
        assert state(db, identity['domain_id']).generation == original + 1
        assert state(db, target.id).generation == 1


@pytest.mark.parametrize('change', ['delete', 'restore', 'visibility', 'revision', 'evidence', 'context'])
def test_direct_sql_input_changes_are_registered(local_library, change):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        if change == 'restore':
            db.get(Source, saved['id']).deleted_at = utcnow(); db.commit()
        before = state(db, identity['domain_id']).generation
        statements = {
            'delete': "UPDATE sources SET deleted_at=CURRENT_TIMESTAMP WHERE id=:id",
            'restore': "UPDATE sources SET deleted_at=NULL WHERE id=:id",
            'visibility': "UPDATE sources SET visibility='app_only' WHERE id=:id",
            'revision': "UPDATE source_revisions SET extracted_text='edited body' WHERE source_id=:id",
            'evidence': "UPDATE evidence_chunks SET text='edited evidence' WHERE revision_id IN (SELECT id FROM source_revisions WHERE source_id=:id)",
            'context': "UPDATE personal_context SET notes='edited note' WHERE source_id=:id",
        }
        db.execute(text(statements[change]), {'id': saved['id']}); db.commit()
        assert state(db, identity['domain_id']).generation > before


def test_task_progress_does_not_dirty_domain(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        before = state(db, identity['domain_id']).generation
        db.execute(text("UPDATE sources SET upload_status='uploading',updated_at=CURRENT_TIMESTAMP WHERE id=:id"), {'id': saved['id']})
        db.commit()
        assert state(db, identity['domain_id']).generation == before


def test_budget_reservation_is_atomic_idempotent_and_persists(local_library):
    _, _, sessions, identity, _ = local_library
    capture_note(local_library)
    owner, domain = identity['owner_id'], identity['domain_id']
    authorization, request = uuid4(), uuid4()
    policy = DomainProcessingPolicy(enabled=True, max_calls=1, max_input_tokens=1000)
    with sessions() as db:
        authorize_processing(db, owner, domain, 'app', policy, authorization); db.commit()
        generation = state(db, domain).generation
        args = dict(generation=generation, request_id=request, input_tokens=1000,
                    now=datetime.now(timezone.utc), manual=True)
        assert reserve_domain_call(db, owner, domain, 'app', **args) == 'reserved'
        db.rollback()
        assert state(db, domain).used_calls == 0
        assert reserve_domain_call(db, owner, domain, 'app', **args) == 'reserved'
        db.commit()
    with sessions() as db:
        assert reserve_domain_call(db, owner, domain, 'app', **args) == 'already_reserved'
        assert state(db, domain).used_calls == 1
        assert state(db, domain).used_input_tokens == 1000
        authorize_processing(db, owner, domain, 'app', policy, authorization)
        assert state(db, domain).used_calls == 1
        with pytest.raises(ApiError):
            authorize_processing(db, owner, domain, 'app', policy, uuid4())
        interrupt_domain_processing(db); db.commit()
        assert state(db, domain).phase == 'unknown'
        assert state(db, domain).used_calls == 1


def test_pause_stale_generation_and_unknown_request_cannot_auto_reserve(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    owner, domain = identity['owner_id'], identity['domain_id']
    with sessions() as db:
        policy = DomainProcessingPolicy(enabled=True, max_calls=2, max_input_tokens=3000)
        authorize_processing(db, owner, domain, 'app', policy, uuid4()); db.commit()
        generation = state(db, domain).generation
        set_processing_paused(db, owner, domain, 'app', True); db.commit()
        args = dict(generation=generation, request_id=uuid4(), input_tokens=1000,
                    now=datetime.now(timezone.utc) + timedelta(seconds=301))
        assert reserve_domain_call(db, owner, domain, 'app', **args) == 'paused'
        db.get(Source, saved['id']).title = 'changed'; db.commit()
        assert reserve_domain_call(db, owner, domain, 'app', **args) == 'stale_generation'
        args['generation'] = state(db, domain).generation
        state(db, domain).phase = 'unknown'; db.commit()
        assert reserve_domain_call(db, owner, domain, 'app', **args) == 'unknown_requires_confirmation'
        assert state(db, domain).used_calls == 0


def test_two_workers_cannot_reserve_the_same_budget(local_library):
    _, _, sessions, identity, _ = local_library
    capture_note(local_library)
    owner, domain = identity['owner_id'], identity['domain_id']
    with sessions() as db:
        authorize_processing(db, owner, domain, 'app',
            DomainProcessingPolicy(enabled=True, max_calls=1, max_input_tokens=1000), uuid4())
        db.commit()
        generation = state(db, domain).generation

    def reserve(_):
        with sessions() as db:
            result = reserve_domain_call(db, owner, domain, 'app', generation=generation,
                request_id=uuid4(), input_tokens=1000, now=datetime.now(timezone.utc), manual=True)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == ['reserved', 'running']
    with sessions() as db:
        assert state(db, domain).used_calls == 1


def test_submission_records_policy_but_not_runtime_usage(local_library):
    _, _, sessions, identity, _ = local_library
    capture_note(local_library)
    with sessions() as db:
        authorize_processing(db, identity['owner_id'], identity['domain_id'], 'app',
            DomainProcessingPolicy(enabled=True, max_calls=2, max_input_tokens=1000), uuid4())
        db.commit()
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        body = json.loads(operation.payload_json)['body']
        assert len(body['processing_preferences']) == 1
        assert 'used_calls' not in operation.payload_json
        assert 'generation' not in body


def test_v8_migration_backs_up_and_rejects_trigger_tampering(local_library):
    paths, engine, _, _, _ = local_library
    with engine.begin() as db:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(db)
        db.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=9')
        validate_change_triggers(db, version=8)
        migrations.require_schema(db, allow_previous=True)
    backup = migrations.migrate(engine, paths.backups)
    assert backup.is_file()
    with engine.connect() as db:
        assert migrations.schema_version(db) == migrations.CURRENT_VERSION
        migrations.require_schema(db)
        db.exec_driver_sql('DROP TRIGGER pka_domain_sources_update'); db.commit()
        with pytest.raises(ValueError): migrations.require_schema(db)


def test_backup_keeps_policy_but_disables_paid_processing_and_clears_state(tmp_path, library):
    paths, identity, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        domain = db.scalar(select(Domain).where(Domain.owner_id == identity['owner_id']))
        authorize_processing(db, identity['owner_id'], domain.id, 'app',
            DomainProcessingPolicy(enabled=True, max_calls=3, max_input_tokens=8000), uuid4())
        db.commit()
    engine.dispose()
    package = tmp_path / 'domain.pka'
    export_backup(paths, package)
    target = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, target, 'r' * 48)
    with closing(sqlite3.connect(target.database)) as db:
        policy = json.loads(db.execute('SELECT policy_json FROM domain_processing_preferences').fetchone()[0])
        assert policy['enabled'] is False and policy['paused'] is True
        assert policy['max_calls'] == policy['max_input_tokens'] == 0
        assert db.execute('SELECT COUNT(*) FROM domain_processing_states').fetchone() == (0,)
