"""Local topic lifecycle and API isolation; all data is synthetic."""
from contextlib import closing
import json
import sqlite3
import zipfile

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.domain_inputs import freeze_domain_inputs, manifest_fingerprint
from app.domain_topics import (ensure_local_topics, prepare_local_topics_once, rebuild_local_topics,
                               topic_directory, topic_sources)
from app.local_backup import export_backup, restore_backup
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.models import (Domain, DomainProcessingState, DomainTopic, DomainTopicMember,
                        KnowledgeTag, Source, TagAssignment, User, utcnow)
from app.errors import ApiError
from app.local_changes import validate_change_triggers
from app import migrations
from app.sync_outbox import prepare_operation
from test_local_changes import local_library, capture_note


def headers():
    return {'Authorization': 'Bearer ' + 'j' * 48}


def labeled(library):
    first = capture_note(library, 'first')
    second = capture_note(library, 'second')
    _, _, sessions, identity, _ = library
    with sessions() as db:
        tag = KnowledgeTag(owner_id=identity['owner_id'], name='检索', dimension='topic', normalized_name='检索')
        db.add(tag); db.flush()
        for saved in (first, second):
            source = db.get(Source, saved['id'])
            db.add(TagAssignment(source_id=source.id, tag_id=tag.id, origin='manual'))
            db.add(TagAssignment(source_id=source.id, tag_id=tag.id, origin='ai', revision_id=source.current_revision_id))
        db.commit()
        return first['id'], second['id'], tag.id


def directory(library, scope='app'):
    _, _, sessions, identity, _ = library
    with sessions() as db:
        return topic_directory(db, identity['owner_id'], identity['domain_id'], scope)


def rebuild(library, scope='app'):
    _, _, sessions, identity, _ = library
    with sessions() as db:
        result = rebuild_local_topics(db, identity['owner_id'], identity['domain_id'], scope)
        db.commit()
        return result


def test_candidates_deduplicate_origins_have_stable_ids_and_do_not_dirty_their_inputs(local_library):
    first, second, _ = labeled(local_library)
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        before = db.get(DomainProcessingState, (identity['domain_id'], 'app')).generation
        fingerprint = manifest_fingerprint(freeze_domain_inputs(db, identity['owner_id'], identity['domain_id']))
    assert rebuild(local_library)['model_calls'] == 0
    result = directory(local_library)
    assert result['topic_count'] == 1 and result['source_count'] == 2
    assert result['items'][0]['source_count'] == 2
    topic = result['items'][0]['id']
    rebuild(local_library)
    assert directory(local_library)['items'][0]['id'] == topic
    with sessions() as db:
        assert db.get(DomainProcessingState, (identity['domain_id'], 'app')).generation == before
        assert manifest_fingerprint(freeze_domain_inputs(db, identity['owner_id'], identity['domain_id'])) == fingerprint
        page = topic_sources(db, identity['owner_id'], identity['domain_id'], topic, 0, 1)
        assert page['next_offset'] == 1
        assert {page['items'][0]['id'], topic_sources(db, identity['owner_id'], identity['domain_id'], topic, 1, 1)['items'][0]['id']} == {first, second}


def test_singleton_and_untagged_sources_are_not_hidden_or_forced_into_topics(local_library):
    capture_note(local_library)
    rebuild(local_library)
    result = directory(local_library)
    assert result['topic_count'] == 0
    assert result['unclassified_count'] == result['source_count'] == 1


def test_sources_can_belong_to_multiple_topics(local_library):
    first, second, _ = labeled(local_library)
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        tag = KnowledgeTag(owner_id=identity['owner_id'], name='评测', dimension='topic', normalized_name='评测')
        db.add(tag); db.flush()
        for source_id in (first, second):
            db.add(TagAssignment(source_id=source_id, tag_id=tag.id, origin='manual'))
        db.commit()
    rebuild(local_library)
    result = directory(local_library)
    assert result['topic_count'] == 2 and result['source_count'] == 2
    assert [item['source_count'] for item in result['items']] == [2, 2]
    assert result['unclassified_count'] == 0


def test_other_owner_cannot_read_domain_topics(local_library):
    labeled(local_library); rebuild(local_library)
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        other = User(email='topic-other@example.test', name='Other')
        db.add(other); db.commit()
        with pytest.raises(ApiError) as error:
            topic_directory(db, other.id, identity['domain_id'], 'app')
        assert error.value.status_code == 404


def test_manual_name_and_exclusion_survive_rebuild_and_change_input_fingerprint(local_library):
    first, _, _ = labeled(local_library)
    rebuild(local_library)
    _, _, sessions, identity, client = local_library
    topic = directory(local_library)['items'][0]['id']
    base = f'/v1/domains/{identity["domain_id"]}/topics/{topic}'
    with sessions() as db:
        before = manifest_fingerprint(freeze_domain_inputs(db, identity['owner_id'], identity['domain_id']))
    assert client.patch(base, json={'name': '人工主题'}, headers=headers()).status_code == 200
    assert client.put(base + f'/members/{first}', json={'included': False}, headers=headers()).status_code == 200
    rebuild(local_library)
    result = directory(local_library)
    assert result['items'][0]['name'] == '人工主题'
    assert result['items'][0]['source_count'] == 1
    assert result['unclassified_count'] == 1
    with sessions() as db:
        assert manifest_fingerprint(freeze_domain_inputs(db, identity['owner_id'], identity['domain_id'])) != before
        assert db.get(DomainTopicMember, (topic, first)).manual is True


@pytest.mark.parametrize('change', ['deleted', 'deleting', 'private', 'moved'])
def test_current_access_filters_members_immediately_before_rebuilding(local_library, change):
    first, _, _ = labeled(local_library)
    rebuild(local_library, 'shareable')
    _, _, sessions, identity, _ = local_library
    topic = directory(local_library, 'shareable')['items'][0]['id']
    with sessions() as db:
        source = db.get(Source, first)
        if change == 'deleted': source.deleted_at = utcnow()
        elif change == 'deleting': source.processing_status = 'deleting'
        elif change == 'private': source.visibility = 'app_only'
        else:
            domain = Domain(owner_id=identity['owner_id'], name='Moved')
            db.add(domain); db.flush(); source.domain_id = domain.id
        db.commit()
        result = topic_sources(db, identity['owner_id'], identity['domain_id'], topic, 0, 40)
        assert first not in {item['id'] for item in result['items']}
    assert directory(local_library, 'shareable')['items'][0]['source_count'] == 1


def test_shareable_candidates_do_not_include_private_only_topic_names(local_library):
    first, second, _ = labeled(local_library)
    _, _, sessions, _, _ = local_library
    with sessions() as db:
        db.get(Source, first).visibility = 'app_only'
        db.get(Source, second).visibility = 'app_only'
        db.commit()
    rebuild(local_library, 'app'); rebuild(local_library, 'shareable')
    assert directory(local_library)['topic_count'] == 1
    assert directory(local_library, 'shareable')['items'] == []


def test_api_manual_hierarchy_and_validation(local_library):
    _, _, _, identity, client = local_library
    base = f'/v1/domains/{identity["domain_id"]}/topics'
    root = client.post(base, json={'name': '主题'}, headers=headers())
    assert root.status_code == 201
    root_id = root.json()['id']
    child = client.post(base, json={'name': '子主题', 'parent_id': root_id}, headers=headers())
    assert child.status_code == 201
    child_id = child.json()['id']
    assert client.post(base, json={'name': '三级', 'parent_id': child_id}, headers=headers()).status_code == 422
    assert client.patch(base + '/' + root_id, json={'parent_id': child_id}, headers=headers()).status_code == 422
    assert client.post(base, json={'name': '  '}, headers=headers()).status_code == 422
    assert client.patch(base + '/' + root_id, json={'name': None}, headers=headers()).status_code == 422
    assert client.get(base + '?limit=0', headers=headers()).status_code == 422
    assert client.get('/v1/domains/not-owned/topics', headers=headers()).status_code == 404
    assert client.get(base).status_code == 401


def test_raw_sql_rejects_cross_domain_members_and_three_levels(local_library):
    first, _, _ = labeled(local_library)
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        other = Domain(owner_id=identity['owner_id'], name='Other')
        db.add(other); db.flush()
        topic = DomainTopic(owner_id=identity['owner_id'], domain_id=other.id, scope='app', name='Other')
        db.add(topic); db.commit()
        with pytest.raises(IntegrityError):
            db.add(DomainTopicMember(topic_id=topic.id, source_id=first)); db.flush()
        db.rollback()
        root = DomainTopic(owner_id=identity['owner_id'], domain_id=other.id, scope='app', name='Root')
        db.add(root); db.flush()
        child = DomainTopic(owner_id=identity['owner_id'], domain_id=other.id, scope='app', name='Child', parent_id=root.id)
        db.add(child); db.commit()
        with pytest.raises(IntegrityError):
            db.add(DomainTopic(owner_id=identity['owner_id'], domain_id=other.id, scope='app', name='Invalid', parent_id=child.id)); db.flush()
        db.rollback()


def test_worker_local_update_settles_and_updates_after_tag_removal(local_library):
    first, second, tag = labeled(local_library)
    _, _, sessions, _, _ = local_library
    with sessions() as db:
        ensure_local_topics(db)
        count = 0
        while prepare_local_topics_once(db):
            count += 1
            assert count < 20
        assert count > 0
        db.execute(delete(TagAssignment).where(TagAssignment.source_id == second)); db.commit()
        assert prepare_local_topics_once(db)
        while prepare_local_topics_once(db): pass
    assert directory(local_library)['topic_count'] == 0
    assert directory(local_library)['unclassified_count'] == 2


def test_permanent_delete_removes_members_and_domain_delete_cleans_state(local_library):
    first, _, _ = labeled(local_library); rebuild(local_library)
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        # Use a source with no dependent revision/file rows for direct deletion.
        extra = Source(owner_id=identity['owner_id'], domain_id=identity['domain_id'],
                       kind='text', title='Extra', coverage='full_text')
        db.add(extra); db.flush()
        topic = db.scalar(select(DomainTopic).where(DomainTopic.domain_id == identity['domain_id']))
        db.add(DomainTopicMember(topic_id=topic.id, source_id=extra.id, manual=True)); db.commit()
        extra_id = extra.id
        db.delete(extra); db.commit()
        assert db.scalar(select(DomainTopicMember).where(DomainTopicMember.source_id == extra_id)) is None
        empty = Domain(owner_id=identity['owner_id'], name='Empty')
        db.add(empty); db.flush()
        db.add(DomainTopic(domain_id=empty.id, owner_id=identity['owner_id'], scope='app', name='Manual', name_fixed=True))
        db.commit()
        empty_id = empty.id
        db.delete(empty); db.commit()
        assert db.scalar(select(DomainProcessingState).where(DomainProcessingState.domain_id == empty_id)) is None
        assert db.scalar(select(DomainTopic).where(DomainTopic.domain_id == empty_id)) is None


def test_v9_upgrade_adds_topic_tables_without_resetting_input_state(local_library):
    paths, engine, _, identity, _ = local_library
    capture_note(local_library)
    with engine.begin() as db:
        from persistence_helpers import remove_full_analysis_schema
        remove_full_analysis_schema(db)
        before = db.exec_driver_sql('SELECT domain_id,scope,generation FROM domain_processing_states ORDER BY domain_id,scope').all()
        db.exec_driver_sql('DROP TRIGGER IF EXISTS pka_node_cache_delete')
        db.exec_driver_sql('DROP TABLE IF EXISTS domain_node_embeddings')
        db.exec_driver_sql('DROP TABLE IF EXISTS domain_node_index_jobs')
        names = db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").scalars().all()
        for name in names:
            if name.startswith(('pka_topic_', 'pka_change_domain_topic', 'pka_node_', 'pka_build_', 'pka_change_domain_summary')):
                db.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for table in ('domain_builds', 'domain_summary_jobs', 'domain_summary_artifacts', 'domain_topic_members', 'domain_topics', 'domain_topic_index_states'):
            db.exec_driver_sql(f'DROP TABLE {table}')
        db.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=10')
        validate_change_triggers(db, version=9)
        migrations.require_schema(db, allow_previous=True)
    backup = migrations.migrate(engine, paths.backups)
    assert backup.is_file()
    with engine.connect() as db:
        assert migrations.schema_version(db) == migrations.CURRENT_VERSION
        assert db.exec_driver_sql('SELECT domain_id,scope,generation FROM domain_processing_states ORDER BY domain_id,scope').all() == before
        assert db.exec_driver_sql('PRAGMA foreign_key_check').all() == []


def test_backup_and_readable_export_preserve_manual_choices_without_runtime_cache(tmp_path, local_library):
    first, _, _ = labeled(local_library)
    rebuild(local_library)
    paths, _, sessions, identity, _ = local_library
    topic = directory(local_library)['items'][0]['id']
    with sessions() as db:
        member = db.get(DomainTopicMember, (topic, first))
        member.manual, member.included = True, False
        db.commit()
        operation = prepare_operation(db, 'domain', identity['domain_id'])
        body = json.loads(operation.payload_json)['body']
        assert body['topics'][0]['id'] == topic
        assert any(m['manual'] and not m['included'] for m in body['topic_members'])
        assert 'domain_topic_index_states' not in body
    # The fixture owns a TestClient, not a host data lock; archive uses a stopped
    # isolated library and does not interact with the user's app.
    package = tmp_path / 'topics.pka'
    export_backup(paths, package)
    target = local_paths(tmp_path / 'restore', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, target, 'q' * 48)
    with closing(sqlite3.connect(target.database)) as db:
        assert db.execute('SELECT manual,included FROM domain_topic_members WHERE topic_id=? AND source_id=?', (topic, first)).fetchone() == (1, 0)
        assert db.execute('SELECT COUNT(*) FROM domain_topic_index_states').fetchone() == (0,)
    readable = tmp_path / 'topics.zip'
    export_readable(paths, readable)
    with zipfile.ZipFile(readable) as archive:
        metadata = json.loads(archive.read('metadata.json'))
        assert metadata['domain_topics'][0]['id'] == topic
        assert any(m['manual'] and not m['included'] for m in metadata['domain_topic_members'])
