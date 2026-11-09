"""v12 plans are disposable; published original-backed results survive restore."""
from dataclasses import replace
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ai, migrations, semantic, domain_builds, domain_summary_jobs
from app.auth import AppPrincipal
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import validate_change_triggers
from app.local_paths import local_paths
from app.models import DomainBuild, DomainSummaryJob, DomainProcessingPreference, Domain, Source, EvidenceChunk, Project
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing
from app.domain_schedule import schedule_domain_once
from services.worker import ai_analysis, domain_summary
from test_local_changes import local_library, capture_note
from test_summary_retrieval import analyzed
from test_domain_builds import enqueue, finish


def test_v11_upgrade_keeps_state_and_adds_index(local_library):
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
            if name.startswith('pka_build_'):
                connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.exec_driver_sql('DROP TABLE domain_builds')
        connection.exec_driver_sql('DROP INDEX ix_node_step_key')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=12')
        validate_change_triggers(connection, version=11)
        migrations.require_schema(connection, allow_previous=True)
    assert migrations.migrate(engine, paths.backups) is not None
    with engine.connect() as connection:
        migrations.require_schema(connection)
        assert connection.exec_driver_sql('SELECT * FROM domain_processing_states ORDER BY domain_id,scope').all() == before
        assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_node_step_key'").scalar()


def test_published_tree_survives_restore_without_jobs_or_new_ai_calls(local_library, tmp_path, monkeypatch):
    paths, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    for module in (ai, ai_analysis, domain_summary, domain_builds, domain_summary_jobs):
        monkeypatch.setattr(module, 'settings', replace(module.settings,
            ai_gateway_url='http://127.0.0.1:8765', ai_gateway_token='synthetic-secret'))
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))
    with sessions() as db:
        source = db.get(Source, saved['id'])
        evidence = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id))
        data = (db, sessions, source, evidence, db.get(Domain, source.domain_id), db.get(Project, identity['project_id']),
                AppPrincipal(source.owner_id), AppPrincipal(str(uuid4())))
        analyzed(data, summary='合成知识加工方法。')
        build, _ = enqueue(data)
        calls = []; finish(data, calls)
        assert build.status == 'completed' and len(calls) == 1
        result = domain_summary_jobs.current_node_result(db, source.owner_id, source.domain_id, 'app')
        assert result['available']
        identifier = result['id']
        authorize_processing(db, source.owner_id, source.domain_id, 'app',
            DomainProcessingPolicy(enabled=True, automatic_updates=True, automatic_max_sources=100,
                automatic_max_steps=20, max_calls=30, max_input_tokens=3000000), uuid4())
        db.commit()
    package = tmp_path / 'tree.pka'
    export_backup(paths, package)
    target = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, target, 'v' * 48)
    restored = make_engine(f'sqlite:///{target.database}')
    try:
        with Session(restored) as db:
            assert db.scalar(select(DomainBuild)) is None and db.scalar(select(DomainSummaryJob)) is None
            result = domain_summary_jobs.current_node_result(db, identity['owner_id'], identity['domain_id'], 'app')
            assert result['available'] and result['id'] == identifier
            assert result['coverage']['cited_source_count'] == 1
            preference = db.get(DomainProcessingPreference, (identity['domain_id'], 'app'))
            policy = DomainProcessingPolicy.model_validate_json(preference.policy_json)
            assert not policy.enabled and not policy.automatic_updates and policy.paused
            assert policy.max_calls == policy.max_input_tokens == 0
            assert not schedule_domain_once(db)
    finally:
        restored.dispose()
