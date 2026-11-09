"""Mapped topic results survive real backups without runnable build records."""
from dataclasses import replace
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ai, domain_builds, domain_node_index as index, domain_summary_jobs, semantic
from app.auth import AppPrincipal
from app.database import make_engine
from app.domain_processing_nodes import processing_view
from app.local_backup import export_backup, restore_backup
from app.local_paths import local_paths
from app.models import (Domain, DomainBuild, DomainNodeEmbedding, DomainNodeIndexJob,
                        DomainSummaryJob, EvidenceChunk, Project, Source)
from services.worker import ai_analysis, domain_summary
from test_domain_retrieval import build
from test_domain_topic_mapping import make_topic
from test_local_changes import capture_note, local_library
from test_summary_retrieval import analyzed


@pytest.mark.parametrize('scope', ['app', 'shareable'])
def test_mapped_topic_restore_keeps_publication_and_rebuilds_only_local_vectors(
        local_library, tmp_path, monkeypatch, scope):
    # A v1 topic-mapped artifact remains readable after the default planner changes.
    monkeypatch.setattr(domain_builds, 'prepare_plan', domain_builds.prepare_legacy_plan)
    paths, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    for module in (ai, ai_analysis, domain_summary, domain_builds, domain_summary_jobs):
        monkeypatch.setattr(module, 'settings', replace(module.settings,
            ai_gateway_url='http://127.0.0.1:8765', ai_gateway_token='synthetic-secret'))
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))
    with sessions() as db:
        source = db.get(Source, saved['id'])
        evidence = db.scalar(select(EvidenceChunk).where(
            EvidenceChunk.revision_id == source.current_revision_id))
        data = (db, sessions, source, evidence, db.get(Domain, source.domain_id),
                db.get(Project, identity['project_id']), AppPrincipal(source.owner_id),
                AppPrincipal(str(uuid4())))
        analyzed(data)
        topic = make_topic(data, scope=scope)
        topic_id = topic.id
        build(data, scope=scope)
        published = domain_summary_jobs.current_node_result(
            db, source.owner_id, source.domain_id, scope, topic_id)
        assert published['available']
        artifact_id = published['id']
        view = processing_view(db, source.owner_id, source.domain_id, scope, artifact_id)
        assert view['available'] and view['published_topic_id'] == topic_id
        assert view['node_id'] != topic_id
        monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
        monkeypatch.setattr(semantic, 'embed_documents', lambda texts: [
            np.asarray([1, 0], dtype=np.float32).tobytes() for _ in texts])
        index.ensure_node_index(db)
        while index.index_node_once(db):
            pass
        assert db.scalar(select(DomainNodeEmbedding).where(
            DomainNodeEmbedding.artifact_id == artifact_id)) is not None
        db.commit()

    package = tmp_path / f'mapped-{scope}.pka'
    export_backup(paths, package)
    target = local_paths(tmp_path / 'restored', tmp_path / 'restored-cache', tmp_path / 'restored-logs')
    restore_backup(package, target, 'v' * 48)
    restored = make_engine(f'sqlite:///{target.database}')
    monkeypatch.setattr(domain_summary, 'call_node_gateway',
                        lambda *args, **kwargs: pytest.fail('恢复及本地索引不得调用收费网关'))
    try:
        with Session(restored) as db:
            assert db.scalar(select(DomainBuild)) is None
            assert db.scalar(select(DomainSummaryJob)) is None
            assert db.scalar(select(DomainNodeEmbedding)) is None
            assert db.scalar(select(DomainNodeIndexJob)) is None
            result = domain_summary_jobs.current_node_result(
                db, identity['owner_id'], identity['domain_id'], scope, topic_id)
            assert result['available'] and result['id'] == artifact_id
            assert result['result'] == published['result']
            assert result['coverage'] == published['coverage']
            restored_view = processing_view(db, identity['owner_id'], identity['domain_id'],
                                            scope, artifact_id)
            assert restored_view == view
            index.ensure_node_index(db)
            while index.index_node_once(db):
                pass
            assert index.node_index_status(db, artifact_id) == 'ready'
            assert db.scalar(select(DomainNodeEmbedding).where(
                DomainNodeEmbedding.artifact_id == artifact_id)) is not None
            assert db.scalar(select(DomainBuild)) is None
            assert db.scalar(select(DomainSummaryJob)) is None
    finally:
        restored.dispose()
