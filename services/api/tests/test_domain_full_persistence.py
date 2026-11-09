"""Whole-domain workflow checkpoints migrate locally and never replay on restore."""
import json
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import migrations
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import validate_change_triggers
from app.local_paths import local_paths
from app.models import AIJob, DomainFullAnalysisJob
from persistence_helpers import remove_full_analysis_schema
from test_local_changes import local_library, capture_note


def add_job(library, status='running'):
    identity = library[3]
    with library[2]() as db:
        job = DomainFullAnalysisJob(id=str(uuid4()), owner_id=identity['owner_id'],
            domain_id=identity['domain_id'], request_sha256='a' * 64,
            snapshot_json=json.dumps({'documents': [], 'synthetic': True}),
            progress_json='{}', budget_json='{"calls":2,"input_token_reservation":200000}',
            status=status)
        db.add(job); db.commit()
        return job.id


def test_v14_upgrade_adds_workflow_without_changing_knowledge(local_library):
    paths, engine = local_library[:2]
    capture_note(local_library)
    with engine.begin() as db:
        before = db.exec_driver_sql('SELECT id,title,current_revision_id FROM sources ORDER BY id').all()
        remove_full_analysis_schema(db)
        db.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=15')
        validate_change_triggers(db, version=14)
        migrations.require_schema(db, allow_previous=True)
    backup = migrations.migrate(engine, paths.backups)
    assert backup is not None
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 14
        assert db.execute("SELECT name FROM sqlite_master WHERE name='domain_full_analysis_jobs'").fetchone() is None
    with engine.connect() as db:
        assert db.exec_driver_sql('SELECT id,title,current_revision_id FROM sources ORDER BY id').all() == before
        assert db.exec_driver_sql('SELECT COUNT(*) FROM domain_full_analysis_jobs').scalar() == 0
        assert migrations.schema_version(db) == migrations.CURRENT_VERSION
        migrations.require_schema(db)
        assert db.exec_driver_sql('PRAGMA integrity_check').scalar() == 'ok'
        assert db.exec_driver_sql('PRAGMA foreign_key_check').first() is None
    assert migrations.migrate(engine, paths.backups) is None


def test_workflow_frozen_scope_is_immutable_but_progress_is_mutable(local_library):
    identifier = add_job(local_library)
    with local_library[2]() as db:
        with pytest.raises(IntegrityError):
            db.execute(text('UPDATE domain_full_analysis_jobs SET snapshot_json=:value WHERE id=:id'),
                       {'value': '{}', 'id': identifier})
        db.rollback()
        job = db.get(DomainFullAnalysisJob, identifier)
        job.progress_json = '{"completed_sources":1}'
        job.status = 'completed'; job.phase = 'completed'
        db.commit()
        assert db.get(DomainFullAnalysisJob, identifier).status == 'completed'


def test_workflow_owner_cannot_cross_domain(local_library):
    identifier = add_job(local_library)
    with local_library[2]() as db:
        original = db.get(DomainFullAnalysisJob, identifier)
        db.add(DomainFullAnalysisJob(id=str(uuid4()), owner_id=str(uuid4()), domain_id=original.domain_id,
            request_sha256='b' * 64, snapshot_json='{}', progress_json='{}', budget_json='{}'))
        with pytest.raises(IntegrityError):
            db.commit()


def test_backup_discards_full_workflow_and_restore_never_resumes_paid_work(local_library, tmp_path):
    paths, _, _, identity, _ = local_library
    saved = capture_note(local_library)
    add_job(local_library)
    package = tmp_path / 'workflow.pka'
    export_backup(paths, package)
    target = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, target, 'v' * 48)
    restored = make_engine(f'sqlite:///{target.database}')
    try:
        with Session(restored) as db:
            assert db.scalar(select(DomainFullAnalysisJob)) is None
            assert db.execute(text('SELECT id FROM sources WHERE id=:id'), {'id': saved['id']}).scalar() == saved['id']
        with restored.connect() as db:
            migrations.require_schema(db)
    finally:
        restored.dispose()


def test_permanent_source_purge_erases_full_scope_and_stops_its_unsent_child(local_library):
    from app.domain_summary_jobs import purge_node_source
    first, second = capture_note(local_library, 'purged-note'), capture_note(local_library, 'retained-note')
    identity = local_library[3]
    identifier, child_id = str(uuid4()), str(uuid4())
    with local_library[2]() as db:
        workflow = DomainFullAnalysisJob(id=identifier, owner_id=identity['owner_id'], domain_id=identity['domain_id'],
            request_sha256='c' * 64, status='running', snapshot_json=json.dumps({'documents': [
                {'source_id': first['id'], 'title': '即将永久删除的合成资料'}, {'source_id': second['id']}]}),
            progress_json=json.dumps({'documents': [{'source_id': second['id'], 'job_id': child_id}]}), budget_json='{}')
        db.add(workflow)
        db.add(AIJob(id=child_id, owner_id=identity['owner_id'], source_id=second['id'], kind='analyze',
            request_key=str(uuid4()), request_sha256='d' * 64, snapshot_json=json.dumps({'full_job_id': identifier}),
            status='pending'))
        db.commit()
        purge_node_source(db, first['id']); db.commit()
        assert db.get(DomainFullAnalysisJob, identifier) is None
        assert db.get(AIJob, child_id).status == 'cancelled'
