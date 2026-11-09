"""v13 local caches migrate safely and never travel in backups."""
from dataclasses import replace
import json
from uuid import uuid4

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ai, migrations, semantic, domain_builds, domain_summary_jobs, domain_node_index as index
from app.auth import AppPrincipal
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import validate_change_triggers
from app.local_paths import local_paths
from app.models import Domain, Source, EvidenceChunk, Project, DomainNodeEmbedding, DomainNodeIndexJob
from services.worker import ai_analysis, domain_summary
from test_local_changes import local_library, capture_note
from test_summary_retrieval import analyzed
from test_domain_retrieval import build


def test_v12_to_v13_preserves_business_state_and_adds_only_caches(local_library):
    paths, engine = local_library[:2]
    capture_note(local_library)
    with engine.begin() as connection:
        from persistence_helpers import remove_full_analysis_schema
        remove_full_analysis_schema(connection)
        before = connection.exec_driver_sql('SELECT * FROM domain_processing_states ORDER BY domain_id,scope').all()
        connection.exec_driver_sql('DROP TRIGGER pka_node_cache_delete')
        connection.exec_driver_sql('DROP TABLE domain_node_embeddings')
        connection.exec_driver_sql('DROP TABLE domain_node_index_jobs')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=13')
        validate_change_triggers(connection, version=12)
        migrations.require_schema(connection, allow_previous=True)
    assert migrations.migrate(engine, paths.backups) is not None
    with engine.connect() as connection:
        migrations.require_schema(connection)
        assert connection.exec_driver_sql('SELECT * FROM domain_processing_states ORDER BY domain_id,scope').all() == before
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM domain_node_embeddings').scalar() == 0


def test_restore_discards_vectors_then_rebuilds_locally_without_paid_calls(local_library, tmp_path, monkeypatch):
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
        analyzed(data); build(data)
        monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
        monkeypatch.setattr(semantic, 'embed_documents', lambda texts: [np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts])
        index.ensure_node_index(db)
        while index.index_node_once(db): pass
        assert db.scalar(select(DomainNodeEmbedding)) is not None
        db.commit()
    package = tmp_path / 'indexed.pka'; export_backup(paths, package)
    target = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, target, 'v' * 48)
    restored = make_engine(f'sqlite:///{target.database}')
    try:
        with Session(restored) as db:
            assert db.scalar(select(DomainNodeEmbedding)) is None and db.scalar(select(DomainNodeIndexJob)) is None
            index.ensure_node_index(db)
            while index.index_node_once(db): pass
            assert db.scalar(select(DomainNodeEmbedding)) is not None
            from app.models import DomainSummaryJob, DomainBuild
            assert db.scalar(select(DomainSummaryJob)) is None and db.scalar(select(DomainBuild)) is None
            assert domain_summary_jobs.current_node_result(db, identity['owner_id'], identity['domain_id'], 'app')['available']
    finally:
        restored.dispose()
