"""v11 migration and backup boundary; synthetic checkpoints only."""
import json
import sqlite3
import zipfile
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import migrations
from app.local_backup import export_backup, restore_backup
from app.local_changes import validate_change_triggers
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.models import Domain, DomainSummaryJob, DomainSummaryArtifact, DomainProcessingState
from app.sync_outbox import prepare_operation
from test_local_changes import local_library, capture_note


def saved_rows(library):
    _, _, sessions, identity, _ = library
    capture_note(library)
    with sessions() as db:
        node = identity['domain_id']
        # Persistence fixture; semantic publication is covered by worker tests.
        snapshot = json.dumps(dict(manifest=dict(sources=[]), coverage=dict(source_count=0)))
        artifact = DomainSummaryArtifact(id=str(uuid4()), owner_id=identity['owner_id'], domain_id=node,
            scope='app', node_id=node, snapshot_json=snapshot, response_json='{"result":"synthetic"}')
        job = DomainSummaryJob(id=str(uuid4()), owner_id=identity['owner_id'], domain_id=node, scope='app',
            generation=1, authorization_id=str(uuid4()), snapshot_json=snapshot,
            gateway_url='http://127.0.0.1:8091', input_tokens=1000)
        db.add_all([job, artifact]); db.commit()
        return job.id, artifact.id


def test_v10_upgrade_preserves_existing_state(local_library):
    paths, engine, _, _, _ = local_library
    capture_note(local_library)
    with engine.begin() as connection:
        from persistence_helpers import remove_full_analysis_schema
        remove_full_analysis_schema(connection)
        before = connection.exec_driver_sql('SELECT * FROM domain_processing_states ORDER BY domain_id,scope').all()
        connection.exec_driver_sql('DROP TRIGGER IF EXISTS pka_node_cache_delete')
        connection.exec_driver_sql('DROP TABLE IF EXISTS domain_node_embeddings')
        connection.exec_driver_sql('DROP TABLE IF EXISTS domain_node_index_jobs')
        for name in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").scalars().all():
            if name.startswith(('pka_node_', 'pka_build_', 'pka_change_domain_summary')):
                connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for table in ('domain_builds', 'domain_summary_jobs', 'domain_summary_artifacts'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=11')
        migrations.require_schema(connection, allow_previous=True)
        validate_change_triggers(connection, version=10)
    assert migrations.migrate(engine, paths.backups) is not None
    with engine.connect() as connection:
        migrations.require_schema(connection)
        assert connection.exec_driver_sql('SELECT * FROM domain_processing_states ORDER BY domain_id,scope').all() == before
        assert migrations.schema_version(connection) == migrations.CURRENT_VERSION


def test_backup_export_and_domain_snapshot_keep_results_not_runnable_jobs(local_library, tmp_path):
    paths, _, sessions, identity, _ = local_library
    job_id, artifact_id = saved_rows(local_library)
    with sessions() as db:
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        payload = json.loads(operation.payload_json)['body']
        assert payload['node_artifacts'][0]['id'] == artifact_id
        assert 'node_jobs' not in payload
    readable = tmp_path / 'readable.zip'
    export_readable(paths, readable)
    with zipfile.ZipFile(readable) as archive:
        metadata = json.loads(archive.read('metadata.json'))
        assert metadata['domain_summary_artifacts'][0]['id'] == artifact_id
        assert 'domain_summary_jobs' not in metadata
    package = tmp_path / 'backup.zip'
    export_backup(paths, package)
    target = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, target, 'k' * 48)
    with sqlite3.connect(target.database) as db:
        assert db.execute('SELECT id FROM domain_summary_artifacts').fetchone()[0] == artifact_id
        assert db.execute('SELECT COUNT(*) FROM domain_summary_jobs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM domain_processing_states').fetchone()[0] == 0


def test_immutable_inputs_results_and_domain_cleanup(local_library):
    job_id, artifact_id = saved_rows(local_library)
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        with pytest.raises(IntegrityError):
            db.execute(text('UPDATE domain_summary_jobs SET input_tokens=1 WHERE id=:id'), dict(id=job_id)); db.flush()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text('UPDATE domain_summary_artifacts SET response_json=\'{}\' WHERE id=:id'), dict(id=artifact_id)); db.flush()
        db.rollback()
        empty = Domain(owner_id=identity['owner_id'], name='Empty')
        db.add(empty); db.flush()
        db.add(DomainSummaryJob(id=str(uuid4()), owner_id=identity['owner_id'], domain_id=empty.id, scope='app',
            generation=0, authorization_id=str(uuid4()), snapshot_json='{}', gateway_url='http://127.0.0.1', input_tokens=1))
        db.commit()
        identifier = empty.id
        db.delete(empty); db.commit()
        assert db.scalar(select(DomainSummaryJob).where(DomainSummaryJob.domain_id == identifier)) is None
