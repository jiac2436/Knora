"""Stopped-library backups with sanitized snapshots and isolated restoration."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base, make_engine
from app.local_import import file_digest
from app.local_paths import LocalPaths, local_paths
from app.local_runtime import data_lock, initialize_identity, validate_credential
from app.migrations import migrate, require_schema, schema_version, snapshot
from app import models  # Registers the exact supported schema.
from app.platform_support import get_platform

MAX_BYTES = 8 * 1024 ** 3
MAX_MANIFEST = 4 * 1024 ** 2
REMOVED_TABLES = ('connection_grants', 'audit_events', 'agent_connections', 'sessions',
                  'password_credentials', 'account_tokens', 'idempotency_records',
                  'deletion_jobs', 'source_search_terms', 'source_search_documents', 'semantic_index_jobs', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs', 'domain_full_analysis_jobs', 'ai_job_steps', 'ai_jobs', 'rag_jobs', 'domain_processing_states', 'domain_topic_index_states', 'domain_summary_jobs', 'domain_builds', 'domain_node_embeddings', 'domain_node_index_jobs')
SYNC_TABLES = ('replica_state', 'sync_entities', 'local_changes', 'sync_operations')


def safe_name(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('恢复包文件路径无效')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts) or PurePosixPath(value).is_absolute():
        raise ValueError('恢复包包含不安全路径')
    if value != 'knowledge.db' and not value.startswith('assets/'):
        raise ValueError('恢复包包含不支持的文件')
    return value


def check_database(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('恢复数据库完整性检查失败')
        entries = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        allowed = set(Base.metadata.tables) | {'schema_migrations'}
        if any(kind == 'view' or (kind == 'table' and name not in allowed) for kind, name in entries):
            raise ValueError('恢复数据库包含不支持的结构')
        identity = db.execute('''SELECT installation.owner_id,installation.library_id
                                 FROM local_installations AS installation
                                 JOIN users AS owner ON owner.id=installation.owner_id
                                 JOIN libraries AS library ON library.id=installation.library_id
                                     AND library.owner_id=installation.owner_id
                                 WHERE installation.id=1''').fetchone()
        if not identity or db.execute('SELECT COUNT(*) FROM local_installations').fetchone()[0] != 1:
            raise ValueError('仅支持已初始化的本地知识库')
    engine = make_engine(f'sqlite:///{path}')
    try:
        with engine.connect() as connection:
            require_schema(connection, allow_previous=True)
            if schema_version(connection) == 1:
                if any(kind == 'trigger' for kind, _ in entries):
                    raise ValueError('恢复数据库包含不支持的结构')
            else:
                from app.local_changes import validate_change_triggers
                validate_change_triggers(connection, version=schema_version(connection))
    finally:
        engine.dispose()
    return identity


def asset_records(path):
    with closing(sqlite3.connect(path)) as db:
        records = db.execute('SELECT id,size_bytes,sha256 FROM file_assets').fetchall()
        records += db.execute("SELECT 'images/' || file_key,size_bytes,sha256 FROM source_images WHERE status='saved'").fetchall()
    result = {}
    for key, size, digest in records:
        name = safe_name('assets/' + key) if isinstance(key, str) else None
        if name is None or type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError('原件清单不完整')
        if name in result and result[name] != (size, digest):
            raise ValueError('原件清单冲突')
        result[name] = (size, digest)
    return result


def check_assets(database, root):
    root = root.resolve()
    for name, (size, digest) in asset_records(database).items():
        path = root / name
        components = [root.joinpath(*PurePosixPath(name).parts[:index])
                      for index in range(1, len(PurePosixPath(name).parts) + 1)]
        if any(component.is_symlink() for component in components) or not path.is_file():
            raise ValueError('恢复包原件缺失或路径不安全')
        if path.stat().st_size != size or file_digest(path) != digest:
            raise ValueError('恢复包原件大小或摘要不一致')


def sanitize(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA secure_delete=ON')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Stop journaling before sanitizing any business rows. Restore creates a
        # new replica baseline, never replays the exporting device's outbox.
        for table in SYNC_TABLES:
            if table in tables:
                db.execute(f'DELETE FROM {table}')
        db.execute('UPDATE local_installations SET session_id=NULL')
        for table in REMOVED_TABLES:
            if table in tables:
                db.execute(f'DELETE FROM {table}')
        if 'domain_processing_preferences' in tables:
            from app.domain_processing import DomainProcessingPolicy
            for domain, scope, policy_json in db.execute('SELECT domain_id,scope,policy_json FROM domain_processing_preferences').fetchall():
                policy = DomainProcessingPolicy.model_validate_json(policy_json)
                policy.enabled, policy.paused = False, True
                policy.automatic_updates = False
                policy.max_calls, policy.max_input_tokens = 0, 0
                db.execute('UPDATE domain_processing_preferences SET policy_json=? WHERE domain_id=? AND scope=?',
                           (policy.model_dump_json(), domain, scope))
        for table in ('document_jobs', 'ocr_jobs', 'web_read_jobs'):
            db.execute(f"UPDATE {table} SET status='failed',completed_at=NULL WHERE status IN ('pending','running')")
            db.execute(f"UPDATE {table} SET last_error=CASE WHEN status='failed' THEN '恢复后请手动重试' ELSE NULL END")
        db.execute("UPDATE source_images SET status='unavailable',failure_reason='恢复后请手动重试图片下载' "
                   "WHERE retrieval_method IN ('wechat_ua','browser_public') AND status IN ('pending','running')")
        db.execute("UPDATE sources SET processing_status=CASE WHEN current_revision_id IS NULL THEN 'failed' ELSE 'usable' END WHERE processing_status IN ('pending','processing')")
        if 'local_changes' in tables and 'sqlite_sequence' in tables:
            db.execute("DELETE FROM sqlite_sequence WHERE name='local_changes'")
        db.commit()
        # Deleted credential bytes must not remain in freelist pages in the archive.
        db.execute('VACUUM')


def export_backup(paths: LocalPaths, output: Path) -> dict:
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(paths.data.resolve()):
        raise ValueError('导出位置不能覆盖已有文件或位于知识库内')
    if not output.parent.is_dir():
        raise ValueError('导出目录不存在')
    with data_lock(paths, exclusive=True, prepare=False):
        if paths.database.is_symlink() or not paths.database.is_file():
            raise ValueError('本地知识库不存在或路径不安全')
        with tempfile.TemporaryDirectory(prefix='.pka-export-', dir=output.parent) as temporary:
            root = Path(temporary)
            database = root / 'knowledge.db'
            snapshot(paths.database, database)
            check_database(database)
            for name, (size, digest) in asset_records(database).items():
                original = paths.data / name
                if any(p.is_symlink() for p in (original, *original.parents)) or not original.is_file():
                    raise ValueError('原件缺失或路径不安全，无法生成完整恢复包')
                copied = root / name
                copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(original, copied)
                get_platform().files.protect(copied)
                if copied.stat().st_size != size or file_digest(copied) != digest:
                    raise ValueError('原件大小或摘要不一致')
            sanitize(database)
            check_database(database)
            names = ['knowledge.db', *sorted(asset_records(database))]
            records = [{'path': name, 'size_bytes': (root / name).stat().st_size,
                        'sha256': file_digest(root / name)} for name in names]
            if sum(item['size_bytes'] for item in records) > MAX_BYTES:
                raise ValueError('恢复包超过当前 8 GiB 验证上限')
            manifest = {'format': 'pka-local-backup', 'version': 1, 'files': records, 'missing_assets': []}
            encoded = json.dumps(manifest, ensure_ascii=False).encode()
            if len(encoded) > MAX_MANIFEST:
                raise ValueError('恢复包清单过大')
            staged = root / 'backup.zip'
            with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', encoded)
                for name in names:
                    archive.write(root / name, name)
            get_platform().files.protect(staged)
            verify_backup(staged)
            # Exclusive publication: never replace a file created during export.
            get_platform().files.publish_file(staged, output)
    return {'format': manifest['format'], 'version': 1, 'files': len(records), 'missing_assets': []}


def unpack(package, root):
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or len(names) > 50000:
            raise ValueError('恢复包包含重复文件或文件过多')
        for entry in entries:
            mode = (entry.external_attr >> 16) & 0o170000
            if entry.is_dir() or mode not in (0, stat.S_IFREG) or entry.flag_bits & 1:
                raise ValueError('恢复包包含链接、特殊文件或加密项')
        if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > MAX_MANIFEST:
            raise ValueError('恢复包清单缺失或过大')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('format') != 'pka-local-backup' or type(manifest.get('version')) is not int or manifest['version'] != 1:
            raise ValueError('恢复包格式或版本不受支持')
        records = manifest.get('files')
        if not isinstance(records, list) or manifest.get('missing_assets') != []:
            raise ValueError('恢复包清单不完整')
        expected = {}
        for item in records:
            if not isinstance(item, dict):
                raise ValueError('恢复包文件记录无效')
            name = safe_name(item.get('path'))
            size, digest = item.get('size_bytes'), item.get('sha256')
            if name in expected or type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64:
                raise ValueError('恢复包大小或摘要记录无效')
            expected[name] = (size, digest)
        if 'knowledge.db' not in expected or set(names) != set(expected) | {'manifest.json'}:
            raise ValueError('恢复包文件与清单不一致')
        if sum(size for size, _ in expected.values()) > MAX_BYTES:
            raise ValueError('恢复包超过当前 8 GiB 验证上限')
        for name, (size, digest) in expected.items():
            if archive.getinfo(name).file_size != size:
                raise ValueError('恢复包文件大小不一致')
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            actual = hashlib.sha256()
            total = 0
            with archive.open(name) as source, path.open('xb') as destination:
                get_platform().files.protect(path)
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    total += len(block)
                    if total > size:
                        raise ValueError('恢复包解压数据超过清单大小')
                    actual.update(block)
                    destination.write(block)
            if total != size or actual.hexdigest() != digest:
                raise ValueError('恢复包文件摘要不一致')
    check_database(root / 'knowledge.db')
    if set(expected) != {'knowledge.db'} | set(asset_records(root / 'knowledge.db')):
        raise ValueError('恢复包原件清单与数据库不一致')
    check_assets(root / 'knowledge.db', root)
    # Reject forged credential-bearing packages, even if their checksums match.
    with closing(sqlite3.connect(root / 'knowledge.db')) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        if any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
               for table in SYNC_TABLES if table in tables):
            raise ValueError('恢复包包含旧设备同步状态或未提交的变更')
        if 'local_changes' in tables and db.execute(
                "SELECT 1 FROM sqlite_sequence WHERE name='local_changes' AND seq<>0").fetchone():
            raise ValueError('恢复包包含旧设备的本地变更序列')
        if any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
               for table in REMOVED_TABLES if table in tables
               and not (table == 'source_search_terms' and version < 14)):
            raise ValueError('恢复包包含凭证或不应重放的记录')
        if db.execute('SELECT 1 FROM local_installations WHERE session_id IS NOT NULL').fetchone():
            raise ValueError('恢复包包含本地会话绑定')
        if any(db.execute(f"SELECT 1 FROM {table} WHERE status IN ('pending','running') LIMIT 1").fetchone()
               for table in ('document_jobs', 'ocr_jobs', 'web_read_jobs')):
            raise ValueError('恢复包包含活动任务')
        if db.execute("SELECT 1 FROM source_images WHERE retrieval_method IN ('wechat_ua','browser_public') "
                      "AND status IN ('pending','running') LIMIT 1").fetchone():
            raise ValueError('恢复包包含活动图片下载任务')
    return manifest


def verify_backup(package: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix='pka-verify-') as temporary:
        manifest = unpack(package, Path(temporary))
    return {'format': manifest['format'], 'version': 1, 'files': len(manifest['files']), 'missing_assets': []}


def publish_directory(stage: Path, destination: Path) -> None:
    """Atomically refuse even an empty destination created during restoration."""
    get_platform().files.publish_directory(stage, destination)


def restore_backup(package: Path, destination: LocalPaths, credential: str) -> dict:
    validate_credential(credential)
    with data_lock(destination, exclusive=True, prepare=False):
        if destination.data.exists() or destination.data.is_symlink():
            raise ValueError('恢复目标必须是尚不存在的新目录，现有知识库不会被覆盖')
        with tempfile.TemporaryDirectory(prefix='.pka-restore-', dir=destination.data.parent) as temporary:
            stage = Path(temporary) / 'data'
            stage.mkdir(mode=0o700)
            unpack(package, stage)
            database = stage / 'knowledge.db'
            with closing(sqlite3.connect(database)) as db:
                # Pre-v14 backups legitimately contain the legacy keyword
                # cache. Accept their format, then rebuild cache from Sources.
                db.execute('DELETE FROM source_search_terms')
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_search_documents'").fetchone():
                    db.execute('DELETE FROM source_search_documents')
                db.execute('UPDATE local_installations SET device_id=?,session_id=NULL WHERE id=1', (str(uuid.uuid4()),))
                db.commit()
            engine = make_engine(f'sqlite:///{database}')
            try:
                migrate(engine, stage / 'backups')
                identity = initialize_identity(engine, credential)
            finally:
                engine.dispose()
            check_database(database)
            if destination.data.exists():
                raise ValueError('恢复目标在准备期间已被创建')
            publish_directory(stage, destination.data)
        destination.prepare()
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', required=True, choices=('export', 'verify', 'restore', 'readable'))
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--log-dir', type=Path)
    args = parser.parse_args()
    try:
        if not args.package.is_absolute() or (args.action != 'verify' and
                any(path is None or not path.is_absolute() for path in (args.data_dir, args.cache_dir, args.log_dir))):
            raise ValueError('必须显式指定绝对恢复包路径和本地目录')
        if args.action == 'verify':
            result = verify_backup(args.package)
        else:
            paths = local_paths(args.data_dir, args.cache_dir, args.log_dir)
            if args.action == 'readable':
                from app.local_readable_export import export_readable
                result = export_readable(paths, args.package)
            else:
                result = (export_backup(paths, args.package) if args.action == 'export' else
                          restore_backup(args.package, paths, sys.stdin.buffer.readline(258).removesuffix(b'\n').decode('ascii')))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, OSError)) else '恢复包处理失败，请检查文件完整性'
        print(json.dumps({'error': message[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
