"""SQLite transaction journal for locally owned knowledge aggregates."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

SYNC_TABLES = frozenset({'replica_state', 'sync_entities', 'local_changes', 'sync_operations'})
TRIGGER_PREFIX = 'pka_change_'


def _entity(table: str, row: str) -> tuple[str, str, str]:
    if table == 'domain_trash':
        return 'domain', f'{row}.domain_id', f'{row}.owner_id'
    if table == 'domain_summary_artifacts':
        return 'domain', f'{row}.domain_id', f'{row}.owner_id'
    if table == 'domain_topics':
        return 'domain', f'{row}.domain_id', f'{row}.owner_id'
    if table == 'domain_topic_members':
        return 'domain', f'(SELECT domain_id FROM domain_topics WHERE id = {row}.topic_id)', f'(SELECT owner_id FROM domain_topics WHERE id = {row}.topic_id)'
    if table == 'domain_processing_preferences':
        return 'domain', f'{row}.domain_id', f'{row}.owner_id'
    if table == 'rag_sessions':
        return 'rag_session', f'{row}.id', f'{row}.owner_id'
    if table == 'rag_turns':
        return 'rag_session', f'{row}.session_id', f'{row}.owner_id'
    if table == 'knowledge_tags':
        return 'tag', f'{row}.id', f'{row}.owner_id'
    if table == 'knowledge_tag_aliases':
        return 'tag', f'{row}.tag_id', f'{row}.owner_id'
    if table in ('domains', 'projects', 'sources'):
        return {'domains': 'domain', 'projects': 'project', 'sources': 'source'}[table], f'{row}.id', f'{row}.owner_id'
    if table in ('evidence_chunks', 'web_read_markdown', 'feishu_document_artifacts'):
        source = f'(SELECT source_id FROM source_revisions WHERE id = {row}.revision_id)'
    else:
        source = f'{row}.source_id'
    return 'source', source, f'(SELECT owner_id FROM sources WHERE id = {source})'


def _gate(owner: str) -> str:
    return f'''EXISTS (SELECT 1 FROM replica_state r JOIN local_installations i ON i.id = r.id
        WHERE r.id = 1 AND r.library_id = i.library_id AND r.device_id = i.device_id
        AND i.owner_id = {owner})'''


def _record(kind: str, identifier: str, change: str) -> str:
    return f'''INSERT INTO sync_entities (entity_kind, entity_id, generation, acknowledged_generation, cloud_version)
        VALUES ('{kind}', {identifier}, 1, 0, 0)
        ON CONFLICT(entity_kind, entity_id) DO UPDATE SET generation = generation + 1;
        INSERT INTO local_changes (entity_kind, entity_id, generation, device_id, change_type, created_at)
        SELECT '{kind}', {identifier}, generation, (SELECT device_id FROM replica_state WHERE id = 1), '{change}', CURRENT_TIMESTAMP
        FROM sync_entities WHERE entity_kind = '{kind}' AND entity_id = {identifier};'''


def _expected_triggers(version: int = 16) -> dict[str, str]:
    from .database import Base
    from . import models  # noqa: F401
    result = {}
    tables = ('domains', 'projects', 'sources', 'source_projects', 'source_revisions', 'evidence_chunks',
              'file_assets', 'personal_context', 'experience_records', 'source_images',
              'web_read_markdown', 'feishu_document_artifacts')
    if version >= 3:
        tables += ('ai_artifacts',)
    if version >= 4:
        tables += ('knowledge_tags', 'knowledge_tag_aliases', 'tag_assignments')
    if version >= 5:
        tables += ('tag_suggestions', 'tag_rejections')
    if version >= 8:
        tables += ('rag_sessions', 'rag_turns')
    if version >= 9:
        tables += ('domain_processing_preferences',)
    if version >= 10:
        tables += ('domain_topics', 'domain_topic_members')
    if version >= 11:
        tables += ('domain_summary_artifacts',)
    if version >= 16:
        tables += ('domain_trash',)
    for table in tables:
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            row = 'OLD' if event == 'DELETE' else 'NEW'
            kind, identifier, owner = _entity(table, row)
            gate = _gate(owner)
            if event == 'UPDATE':
                ignored = {'created_at', 'updated_at'}
                if table == 'sources':
                    ignored |= {'processing_status', 'upload_status'}
                columns = [column.name for column in Base.metadata.tables[table].columns if column.name not in ignored]
                changes = [f'OLD.{column} IS NOT NEW.{column}' for column in columns]
                if table == 'sources':
                    changes.append("(OLD.processing_status IS NOT NEW.processing_status AND (OLD.processing_status IN ('deleting', 'deleted') OR NEW.processing_status IN ('deleting', 'deleted')))")
                gate += ' AND (' + ' OR '.join(changes) + ')'
            change = 'delete' if table in ('domains', 'projects', 'sources', 'knowledge_tags', 'rag_sessions') and event == 'DELETE' else 'upsert'
            name = f'{TRIGGER_PREFIX}{table}_{event.lower()}'
            result[name] = f'CREATE TRIGGER {name} AFTER {event} ON {table} WHEN {gate} BEGIN {_record(kind, identifier, change)} END'
            # A reassigned child changes both aggregates, including the previous source.
            if event == 'UPDATE' and table not in ('domains', 'projects', 'sources', 'knowledge_tags', 'rag_sessions'):
                old_kind, old_id, old_owner = _entity(table, 'OLD')
                name += '_previous'
                result[name] = f'CREATE TRIGGER {name} AFTER UPDATE ON {table} WHEN {_gate(old_owner)} AND {old_id} IS NOT {identifier} BEGIN {_record(old_kind, old_id, "upsert")} END'
    fields = ('id', 'library_id', 'device_id', 'entity_kind', 'entity_id', 'generation',
              'base_version', 'payload_json', 'payload_sha256')
    immutable = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
    result['pka_change_sync_operation_immutable'] = f'''CREATE TRIGGER pka_change_sync_operation_immutable
        BEFORE UPDATE ON sync_operations WHEN ({immutable})
        OR (OLD.status = 'acknowledged' AND (NEW.status IS NOT OLD.status OR NEW.cloud_version IS NOT OLD.cloud_version))
        OR (OLD.status = 'prepared' AND NOT ((NEW.status = 'prepared' AND NEW.cloud_version IS OLD.cloud_version)
        OR (NEW.status = 'acknowledged' AND NEW.cloud_version IS (OLD.base_version + 1))))
        BEGIN SELECT RAISE(ABORT, '同步操作的内容或确认状态不可修改'); END'''
    if version >= 9:
        from .domain_changes import expected_input_triggers
        result.update(expected_input_triggers())
    if version >= 10:
        from .domain_topic_changes import expected_topic_triggers
        result.update(expected_topic_triggers())
    if version >= 11:
        result['pka_node_artifact_immutable'] = """CREATE TRIGGER pka_node_artifact_immutable
            BEFORE UPDATE ON domain_summary_artifacts
            BEGIN SELECT RAISE(ABORT, '知识域概览产物不可修改'); END"""
        result['pka_node_domain_delete'] = """CREATE TRIGGER pka_node_domain_delete BEFORE DELETE ON domains BEGIN
            DELETE FROM domain_summary_jobs WHERE domain_id=OLD.id;
            DELETE FROM domain_summary_artifacts WHERE domain_id=OLD.id; END"""
        result['pka_node_job_immutable'] = """CREATE TRIGGER pka_node_job_immutable BEFORE UPDATE ON domain_summary_jobs
            WHEN OLD.id IS NOT NEW.id OR OLD.owner_id IS NOT NEW.owner_id OR OLD.domain_id IS NOT NEW.domain_id
            OR OLD.scope IS NOT NEW.scope OR OLD.generation IS NOT NEW.generation
            OR OLD.authorization_id IS NOT NEW.authorization_id OR OLD.snapshot_json IS NOT NEW.snapshot_json
            OR OLD.gateway_url IS NOT NEW.gateway_url OR OLD.input_tokens IS NOT NEW.input_tokens
            OR OLD.confirmed_unknown IS NOT NEW.confirmed_unknown OR OLD.created_at IS NOT NEW.created_at
            BEGIN SELECT RAISE(ABORT, '概览任务输入与预算不可修改'); END"""
        for table in ('domain_summary_jobs', 'domain_summary_artifacts'):
            name = 'pka_node_owner_' + table
            result[name] = f"""CREATE TRIGGER {name} BEFORE INSERT ON {table}
                WHEN NOT EXISTS (SELECT 1 FROM domains WHERE id=NEW.domain_id AND owner_id=NEW.owner_id)
                BEGIN SELECT RAISE(ABORT, '概览必须属于当前知识域'); END"""
    if version >= 12:
        result['pka_build_domain_delete'] = """CREATE TRIGGER pka_build_domain_delete BEFORE DELETE ON domains BEGIN
            DELETE FROM domain_builds WHERE domain_id=OLD.id; END"""
        result['pka_build_owner'] = """CREATE TRIGGER pka_build_owner BEFORE INSERT ON domain_builds
            WHEN NOT EXISTS (SELECT 1 FROM domains WHERE id=NEW.domain_id AND owner_id=NEW.owner_id)
            BEGIN SELECT RAISE(ABORT, '加工计划必须属于当前知识域'); END"""
        result['pka_build_immutable'] = """CREATE TRIGGER pka_build_immutable BEFORE UPDATE ON domain_builds
            WHEN OLD.id IS NOT NEW.id OR OLD.owner_id IS NOT NEW.owner_id OR OLD.domain_id IS NOT NEW.domain_id
            OR OLD.scope IS NOT NEW.scope OR OLD.generation IS NOT NEW.generation
            OR OLD.authorization_id IS NOT NEW.authorization_id OR OLD.gateway_url IS NOT NEW.gateway_url
            OR OLD.plan_json IS NOT NEW.plan_json OR OLD.confirmed_unknown IS NOT NEW.confirmed_unknown
            OR OLD.created_at IS NOT NEW.created_at
            BEGIN SELECT RAISE(ABORT, '加工计划与授权不可修改'); END"""
    if version >= 13:
        result['pka_node_cache_delete'] = """CREATE TRIGGER pka_node_cache_delete BEFORE DELETE ON domain_summary_artifacts BEGIN
            DELETE FROM domain_node_embeddings WHERE artifact_id=OLD.id;
            DELETE FROM domain_node_index_jobs WHERE artifact_id=OLD.id; END"""
    if version >= 15:
        result['pka_full_analysis_owner'] = """CREATE TRIGGER pka_full_analysis_owner
            BEFORE INSERT ON domain_full_analysis_jobs
            WHEN NOT EXISTS (SELECT 1 FROM domains WHERE id=NEW.domain_id AND owner_id=NEW.owner_id)
            BEGIN SELECT RAISE(ABORT, '全量分析必须属于当前知识域'); END"""
        result['pka_full_analysis_immutable'] = """CREATE TRIGGER pka_full_analysis_immutable
            BEFORE UPDATE ON domain_full_analysis_jobs
            WHEN OLD.id IS NOT NEW.id OR OLD.owner_id IS NOT NEW.owner_id OR OLD.domain_id IS NOT NEW.domain_id
            OR OLD.request_sha256 IS NOT NEW.request_sha256 OR OLD.snapshot_json IS NOT NEW.snapshot_json
            OR OLD.created_at IS NOT NEW.created_at
            BEGIN SELECT RAISE(ABORT, '全量分析范围与请求不可修改'); END"""
        result['pka_full_analysis_domain_delete'] = """CREATE TRIGGER pka_full_analysis_domain_delete
            BEFORE DELETE ON domains BEGIN
            DELETE FROM domain_full_analysis_jobs WHERE domain_id=OLD.id; END"""
    return result


EXPECTED_TRIGGER_SQL = _expected_triggers()
V2_TRIGGER_SQL = _expected_triggers(2)
V3_TRIGGER_SQL = _expected_triggers(3)
V4_TRIGGER_SQL = _expected_triggers(4)
V7_TRIGGER_SQL = _expected_triggers(7)
V8_TRIGGER_SQL = _expected_triggers(8)
V9_TRIGGER_SQL = _expected_triggers(9)
V10_TRIGGER_SQL = _expected_triggers(10)
V11_TRIGGER_SQL = _expected_triggers(11)
V12_TRIGGER_SQL = _expected_triggers(12)
V13_TRIGGER_SQL = _expected_triggers(13)
V14_TRIGGER_SQL = _expected_triggers(14)
V15_TRIGGER_SQL = _expected_triggers(15)


def trigger_sql(version: int) -> dict[str, str]:
    return {2: V2_TRIGGER_SQL, 3: V3_TRIGGER_SQL, 4: V4_TRIGGER_SQL, 5: V7_TRIGGER_SQL, 6: V7_TRIGGER_SQL, 7: V7_TRIGGER_SQL, 8: V8_TRIGGER_SQL, 9: V9_TRIGGER_SQL, 10: V10_TRIGGER_SQL, 11: V11_TRIGGER_SQL, 12: V12_TRIGGER_SQL, 13: V13_TRIGGER_SQL, 14: V14_TRIGGER_SQL, 15: V15_TRIGGER_SQL, 16: EXPECTED_TRIGGER_SQL}[version]


def install_change_triggers(connection: Connection, *, version: int = 16) -> None:
    expected = trigger_sql(version)
    existing = dict(connection.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").all())
    if set(existing) - set(expected):
        raise ValueError('本地数据库包含未知触发器')
    for name, sql in expected.items():
        if name in existing:
            if _normalize_sql(existing[name]) != _normalize_sql(sql):
                raise ValueError('本地变更记录触发器已被修改')
        else:
            connection.exec_driver_sql(sql)


def _normalize_sql(sql: str) -> str:
    return ' '.join(sql.strip().rstrip(';').split())


def validate_change_triggers(connection: Connection, *, version: int = 16) -> None:
    expected = trigger_sql(version)
    rows = connection.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").all()
    actual = dict(rows)
    if set(actual) != set(expected) or any(
        not actual[name] or _normalize_sql(actual[name]) != _normalize_sql(sql)
        for name, sql in expected.items()
    ):
        raise ValueError('本地变更记录触发器不完整或已被修改')


def initialize_journal(connection: Connection, library_id: str, device_id: str, *, version: int = 16) -> None:
    """Bind once and record a baseline without changing existing entity identities."""
    if connection.dialect.name != 'sqlite':
        raise ValueError('本地变更记录仅支持 SQLite')
    installation = connection.execute(text('SELECT library_id, device_id, owner_id FROM local_installations WHERE id = 1')).first()
    if installation is None or installation.library_id != library_id or installation.device_id != device_id:
        raise ValueError('本地变更记录身份与设备绑定不一致')
    replica = connection.execute(text('SELECT library_id, device_id FROM replica_state WHERE id = 1')).first()
    if replica is not None:
        if replica.library_id != library_id or replica.device_id != device_id:
            raise ValueError('不能切换已有同步记录的库或设备身份')
        return
    validate_change_triggers(connection, version=version)
    connection.execute(text('INSERT INTO replica_state (id, library_id, device_id, applied_sequence, protocol_version) VALUES (1, :library, :device, 0, 1)'),
                       {'library': library_id, 'device': device_id})
    entities = [('domains', 'domain'), ('projects', 'project'), ('sources', 'source')]
    if version >= 4:
        entities.append(('knowledge_tags', 'tag'))
    if version >= 8:
        entities.append(('rag_sessions', 'rag_session'))
    for table, kind in entities:
        identifiers = connection.execute(text(f'SELECT id FROM {table} WHERE owner_id = :owner ORDER BY id'), {'owner': installation.owner_id}).scalars().all()
        for identifier in identifiers:
            parameters = {'kind': kind, 'identifier': identifier, 'device': device_id}
            connection.execute(text('INSERT INTO sync_entities (entity_kind, entity_id, generation, acknowledged_generation, cloud_version) VALUES (:kind, :identifier, 1, 0, 0)'), parameters)
            connection.execute(text("INSERT INTO local_changes (entity_kind, entity_id, generation, device_id, change_type, created_at) VALUES (:kind, :identifier, 1, :device, 'upsert', CURRENT_TIMESTAMP)"), parameters)
