"""Immutable submission snapshots and safe acknowledgements; no cloud calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import (Domain, EvidenceChunk, ExperienceRecord, FeishuDocumentArtifact,
                        FileAsset, LocalChange, LocalInstallation, Project, ReplicaState,
                        Source, SourceImage, SourceRevision, SyncEntityState, SyncOperation,
                        WebReadMarkdown, User)
from app.sync_outbox import acknowledge_operation, prepare_operation
from test_local_changes import capture_note, changes, local_library, state


def prepare_dependencies(db, identity):
    for kind, identifier in [('domain', identity['domain_id']), ('project', identity['project_id'])]:
        prepare_operation(db, kind, identifier)


def test_fixed_snapshot_survives_edits_and_old_ack_leaves_new_generation_dirty(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        prepare_dependencies(db, identity)
        operation = prepare_operation(db, 'source', saved['id'])
        operation_id, original, generation, digest = operation.id, operation.payload_json, operation.generation, operation.payload_sha256
        assert digest == hashlib.sha256(original.encode()).hexdigest()
        assert operation.base_version == 0
        assert operation.library_id == identity['library_id'] and operation.device_id == identity['device_id']
        assert json.loads(original)['body']['revisions'][0]['origin_display_version'] == 1
        db.commit()
    with sessions() as db:
        db.get(Source, saved['id']).title = '后续本地修改'
        db.commit()
    with sessions() as db:
        repeated = prepare_operation(db, 'source', saved['id'])
        assert (repeated.id, repeated.payload_json, repeated.generation) == (operation_id, original, generation)
        result = acknowledge_operation(db, operation_id, 1, digest)
        assert result['dirty'] is True
        assert state(db, 'source', saved['id']).acknowledged_generation == generation
        db.commit()
    with sessions() as db:
        next_operation = prepare_operation(db, 'source', saved['id'])
        assert next_operation.id != operation_id
        assert next_operation.base_version == 1 and next_operation.generation > generation
        assert json.loads(next_operation.payload_json)['body']['entity']['title'] == '后续本地修改'
        db.commit()


def test_ack_replay_is_idempotent_even_after_a_newer_ack(local_library):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        first = prepare_operation(db, 'domain', identity['domain_id'])
        first_id, first_hash = first.id, first.payload_sha256
        assert acknowledge_operation(db, first.id, 1, first_hash)['dirty'] is False
        db.commit()
    with sessions() as db:
        db.get(Domain, identity['domain_id']).name = '新领域名'
        db.commit()
    with sessions() as db:
        second = prepare_operation(db, 'domain', identity['domain_id'])
        assert acknowledge_operation(db, second.id, 2, second.payload_sha256)['dirty'] is False
        db.commit()
    with sessions() as db:
        result = acknowledge_operation(db, first_id, 1, first_hash)
        assert result['status'] == 'acknowledged' and result['dirty'] is False
        assert state(db, 'domain', identity['domain_id']).cloud_version == 2
        assert state(db, 'domain', identity['domain_id']).acknowledged_generation == second.generation
        with pytest.raises(ValueError, match='待提交'):
            prepare_operation(db, 'domain', identity['domain_id'])
        db.rollback()


@pytest.mark.parametrize('version,hash_value', [(0, None), (2, None), (True, None), ('1', None), (1, '0' * 64)])
def test_invalid_ack_does_not_clear_modifications(local_library, version, hash_value):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        identifier, digest = operation.id, operation.payload_sha256
        db.commit()
    with sessions() as db:
        with pytest.raises(ValueError):
            acknowledge_operation(db, identifier, version, hash_value if hash_value is not None else digest)
        db.rollback()
    with sessions() as db:
        assert db.get(SyncOperation, identifier).status == 'prepared'
        assert state(db, 'domain', identity['domain_id']).cloud_version == 0
        assert state(db, 'domain', identity['domain_id']).acknowledged_generation == 0


def test_prepare_and_ack_belong_to_caller_transaction(local_library):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        abandoned_id = operation.id
        db.rollback()
    with sessions() as db:
        assert db.get(SyncOperation, abandoned_id) is None
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        identifier, digest = operation.id, operation.payload_sha256
        db.commit()
    with sessions() as db:
        acknowledge_operation(db, identifier, 1, digest)
        db.rollback()
    with sessions() as db:
        assert db.get(SyncOperation, identifier).status == 'prepared'
        assert state(db, 'domain', identity['domain_id']).acknowledged_generation == 0


def test_concurrent_prepare_reuses_one_operation(local_library):
    _, _, sessions, identity, _ = local_library
    barrier = Barrier(2)

    def prepare():
        with sessions() as db:
            barrier.wait(timeout=10)
            operation = prepare_operation(db, 'domain', identity['domain_id'])
            result = (operation.id, operation.payload_json, operation.payload_sha256)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as threads:
        futures = [threads.submit(prepare) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0] == results[1]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1


@pytest.mark.parametrize('field,value', [('payload_json', '{}'), ('payload_sha256', '0' * 64),
                                         ('entity_id', 'other'), ('generation', 999), ('base_version', 999)])
def test_database_rejects_rewriting_prepared_operation(local_library, field, value):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        identifier, original = operation.id, operation.payload_json
        db.commit()
    with sessions() as db:
        with pytest.raises(IntegrityError):
            db.execute(update(SyncOperation).where(SyncOperation.id == identifier).values(**{field: value}))
            db.commit()
        db.rollback()
        assert db.get(SyncOperation, identifier).payload_json == original


def test_source_preparation_requires_domain_and_project_dependencies(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        with pytest.raises(ValueError, match='先准备'):
            prepare_operation(db, 'source', saved['id'])
        db.rollback()
        domain = prepare_operation(db, 'domain', identity['domain_id'])
        db.commit()
        with pytest.raises(ValueError, match='先准备'):
            prepare_operation(db, 'source', saved['id'])
        db.rollback()
        project = prepare_operation(db, 'project', identity['project_id'])
        operation = prepare_operation(db, 'source', saved['id'])
        dependencies = json.loads(operation.payload_json)['body']['dependencies']
        assert {(item['entity_kind'], item['operation_id']) for item in dependencies} == {
            ('domain', domain.id), ('project', project.id)}
        db.commit()


def test_snapshot_contains_history_evidence_assets_and_knowledge_only(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    secret = 'Bearer synthetic-provider-credential-do-not-sync'
    with sessions() as db:
        source = db.get(Source, saved['id'])
        first_revision = source.current_revision_id
        second_revision = SourceRevision(source_id=source.id, version=2,
                                         extracted_text='第二版合成正文', parser_version='synthetic')
        db.add(second_revision)
        db.flush()
        source.current_revision_id = second_revision.id
        db.add_all([
            EvidenceChunk(revision_id=second_revision.id, ordinal=0, text='第二版证据', locator_end=5),
            FileAsset(source_id=source.id, filename='synthetic.csv', size_bytes=7, sha256='a' * 64),
            SourceImage(id='synthetic-image', source_id=source.id, revision_id=first_revision, ordinal=0,
                        status='saved', failure_reason=secret, caption='图注', ocr_text='图像文字',
                        file_key='synthetic-image-file', sha256='b' * 64, size_bytes=9, mime_type='image/png'),
            WebReadMarkdown(revision_id=first_revision, markdown='# 合成网页正文'),
            FeishuDocumentArtifact(revision_id=first_revision, document_id='synthetic-document',
                                   document_revision_id=9, cli_version='synthetic'),
            ExperienceRecord(owner_id=identity['owner_id'], source_id=source.id,
                             project_id=identity['project_id'], result='success', notes='实践结果')])
        db.commit()
    with sessions() as db:
        prepare_dependencies(db, identity)
        operation = prepare_operation(db, 'source', saved['id'])
        body = json.loads(operation.payload_json)['body']
        assert {row['origin_display_version'] for row in body['revisions']} == {1, 2}
        assert all('version' not in row for row in body['revisions'])
        assert len(body['evidence']) == 2
        assert body['files'][0]['sha256'] == 'a' * 64
        assert body['images'][0]['file_key'] == 'synthetic-image-file'
        assert body['images'][0]['caption'] == '图注' and body['images'][0]['ocr_text'] == '图像文字'
        assert secret not in operation.payload_json
        assert body['images'][0]['failure_reason'] in {'', None}
        assert body['context'][0]['notes'] == '合成补充'
        assert body['experiences'][0]['notes'] == '实践结果'
        assert body['web_markdown'][0]['markdown'] == '# 合成网页正文'
        assert body['feishu_artifacts'][0]['document_id'] == 'synthetic-document'
        assert not {'sessions', 'credentials', 'semantic_index', 'search_terms', 'jobs'} & body.keys()
        assert db.get(SourceImage, 'synthetic-image').failure_reason == secret
        db.commit()


@pytest.mark.parametrize('permanent', [False, True])
def test_deleted_source_snapshot_is_minimal_and_contains_no_knowledge(local_library, permanent):
    _, _, sessions, _, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        source = db.get(Source, saved['id'])
        source.deleted_at = datetime.now(timezone.utc)
        source.processing_status = 'deleted' if permanent else 'usable'
        db.commit()
    with sessions() as db:
        operation = prepare_operation(db, 'source', saved['id'])
        body = json.loads(operation.payload_json)['body']
        assert body['deleted'] is True and body['permanent'] is permanent
        assert body['entity']['id'] == saved['id']
        assert set(body) == {'deleted', 'permanent', 'entity', 'dependencies'}
        assert '合成正文' not in operation.payload_json and saved['title'] not in operation.payload_json
        db.commit()


def test_hard_deleted_entity_can_prepare_tombstone(local_library):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        project = db.get(Project, identity['project_id'])
        db.delete(project)
        db.commit()
    with sessions() as db:
        operation = prepare_operation(db, 'project', identity['project_id'])
        assert json.loads(operation.payload_json)['body']['deleted'] is True
        assert json.loads(operation.payload_json)['body']['permanent'] is True
        db.commit()


def test_invalid_entity_and_unbound_replica_are_refused(local_library):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        for kind, identifier in [('unknown', identity['domain_id']), ('source', 'does-not-exist'), ('domain', '')]:
            with pytest.raises(ValueError):
                prepare_operation(db, kind, identifier)
            db.rollback()
        db.get(ReplicaState, 1).device_id = 'mismatched-device'
        db.commit()
    with sessions() as db:
        with pytest.raises(ValueError, match='身份'):
            prepare_operation(db, 'domain', identity['domain_id'])
        db.rollback()
        with pytest.raises(ValueError):
            acknowledge_operation(db, 'missing-operation', 1, '0' * 64)
        db.rollback()


def test_ack_refuses_unknown_operation_and_wrong_base_version(local_library):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        with pytest.raises(ValueError, match='不存在'):
            acknowledge_operation(db, 'unknown-operation', 1, '0' * 64)
        db.rollback()
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        identifier, digest = operation.id, operation.payload_sha256
        db.commit()
    with sessions() as db:
        state(db, 'domain', identity['domain_id']).cloud_version = 4
        db.commit()
    with sessions() as db:
        with pytest.raises(ValueError, match='基础版本'):
            acknowledge_operation(db, identifier, 1, digest)
        db.rollback()
        assert db.get(SyncOperation, identifier).status == 'prepared'
        assert state(db, 'domain', identity['domain_id']).acknowledged_generation == 0


def test_experience_project_is_a_required_dependency(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        extra = Project(owner_id=identity['owner_id'], name='独立实践项目')
        db.add(extra)
        db.flush()
        extra_id = extra.id
        db.add(ExperienceRecord(owner_id=identity['owner_id'], source_id=saved['id'],
                                project_id=extra_id, result='success'))
        db.commit()
    with sessions() as db:
        prepare_dependencies(db, identity)
        db.commit()
        with pytest.raises(ValueError, match='先准备'):
            prepare_operation(db, 'source', saved['id'])
        db.rollback()
        prepare_operation(db, 'project', extra_id)
        operation = prepare_operation(db, 'source', saved['id'])
        dependencies = json.loads(operation.payload_json)['body']['dependencies']
        assert extra_id in {item['entity_id'] for item in dependencies}
        db.commit()


def test_prepare_refreshes_business_rows_cached_before_external_edit(local_library):
    _, _, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        prepare_dependencies(db, identity)
        db.commit()
        stale_source = db.get(Source, saved['id'])
        stale_revision = db.get(SourceRevision, stale_source.current_revision_id)
        original_generation = state(db, 'source', saved['id']).generation
        assert stale_source.title == saved['title']
        with sessions() as writer:
            writer.get(Source, saved['id']).title = '另一连接的新标题'
            writer.get(SourceRevision, stale_revision.id).extracted_text = '另一连接的新正文'
            writer.commit()
        operation = prepare_operation(db, 'source', saved['id'])
        body = json.loads(operation.payload_json)['body']
        assert operation.generation > original_generation
        assert body['entity']['title'] == '另一连接的新标题'
        assert body['revisions'][0]['extracted_text'] == '另一连接的新正文'
        db.commit()
