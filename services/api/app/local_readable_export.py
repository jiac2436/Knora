"""Portable, readable knowledge exports; deliberately not restoration packages."""
from __future__ import annotations

from contextlib import closing
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

from app.local_backup import MAX_BYTES, MAX_MANIFEST, asset_records, check_assets, check_database, sanitize
from app.local_import import file_digest
from app.local_paths import LocalPaths
from app.local_runtime import data_lock
from app.migrations import snapshot
from app.platform_support import get_platform

FORMAT = 'pka-readable-export'
DOCUMENT_EXTENSIONS = frozenset(('pdf', 'doc', 'docx', 'xls', 'xlsx', 'csv', 'tsv', 'txt',
                                'md', 'markdown', 'ppt', 'pptx', 'rtf', 'odt', 'ods', 'odp',
                                'json', 'yaml', 'yml'))
IMAGE_EXTENSIONS = frozenset(('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tif', 'tiff',
                             'heic', 'heif', 'avif'))
IMAGE_MIME_SUFFIX = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
                     'image/webp': '.webp', 'image/bmp': '.bmp', 'image/tiff': '.tiff',
                     'image/heic': '.heic', 'image/heif': '.heif', 'image/avif': '.avif'}
ASSET_SUFFIX = '(?:\\.(?:' + '|'.join(sorted(DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS)) + '))?'
FILE_PATH = re.compile(r'(?:index\.md|metadata\.json|sources/[0-9]{6,}\.md|'
                       r'versions/[0-9]{6,}-v[0-9]{6,}\.md|rag/[0-9]{6,}\.md|'
                       r'assets/[0-9]{6,}-(?:original|image-[0-9]{6,})' + ASSET_SUFFIX + r')\Z')


def safe_suffix(filename: str | None, allowed: frozenset[str]) -> str:
    suffix = Path(filename or '').suffix.lower()
    return suffix if suffix.removeprefix('.') in allowed else ''


def image_suffix(image: dict) -> str:
    mime = (image['mime_type'] or '').split(';', 1)[0].strip().lower()
    return IMAGE_MIME_SUFFIX.get(mime, safe_suffix(image['file_key'], IMAGE_EXTENSIONS))


def heading(value: str) -> str:
    """User text cannot introduce links, HTML, or extra Markdown headings."""
    escaped = html.escape(str(value), quote=False).replace('\r', ' ').replace('\n', ' ')
    return re.sub(r'([\\`*_{}\[\]()#+.!|~>-])', r'\\\1', escaped)


def literal(value: str | None) -> str:
    # Indented code blocks retain Unicode and treat HTML and URLs as plain text.
    normalized = (value or '').replace('\r\n', '\n').replace('\r', '\n')
    return '\n'.join('    ' + line for line in normalized.split('\n')) + '\n'


def rows(db, table, column, value, order=''):
    # All identifiers originate from fixed call sites, never user data.
    sql = f'SELECT * FROM {table} WHERE {column}=?'
    if order:
        sql += f' ORDER BY {order}'
    return [dict(row) for row in db.execute(sql, (value,))]


def render_library(database: Path, snapshot_root: Path, output_root: Path) -> dict:
    metadata = {'format': FORMAT, 'version': 1, 'sources': [], 'knowledge_tags': [], 'rag_sessions': [],
                'domain_summary_artifacts': [], 'domain_processing_preferences': [], 'domain_topics': [], 'domain_topic_members': []}
    index = ['# 知识库可读导出', '', '此包包含 Markdown、业务元数据和原件，不能用于客户端恢复。', '']
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        tables = {item[0] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'domain_summary_artifacts' in tables:
            metadata['domain_summary_artifacts'] = [dict(item) for item in db.execute(
                'SELECT * FROM domain_summary_artifacts WHERE owner_id=(SELECT owner_id FROM local_installations WHERE id=1) ORDER BY id')]
        if 'domain_topics' in tables:
            metadata['domain_topics'] = [dict(item) for item in db.execute(
                'SELECT * FROM domain_topics WHERE owner_id=(SELECT owner_id FROM local_installations WHERE id=1) ORDER BY id')]
            metadata['domain_topic_members'] = [dict(item) for item in db.execute(
                'SELECT m.* FROM domain_topic_members m JOIN domain_topics t ON t.id=m.topic_id '
                'WHERE t.owner_id=(SELECT owner_id FROM local_installations WHERE id=1) ORDER BY m.topic_id,m.source_id')]
        if 'domain_processing_preferences' in tables:
            metadata['domain_processing_preferences'] = [dict(item) for item in db.execute(
                'SELECT * FROM domain_processing_preferences WHERE owner_id='
                '(SELECT owner_id FROM local_installations WHERE id=1) ORDER BY domain_id,scope')]
        if 'knowledge_tags' in tables:
            metadata['knowledge_tags'] = [dict(item) for item in db.execute(
                'SELECT * FROM knowledge_tags WHERE owner_id=(SELECT owner_id FROM local_installations WHERE id=1) ORDER BY id')]
            for tag in metadata['knowledge_tags']:
                tag['aliases'] = [alias for alias in rows(db, 'knowledge_tag_aliases', 'tag_id', tag['id'], 'id')
                                  if alias['owner_id'] == tag['owner_id']]
        if 'rag_sessions' in tables:
            sessions = db.execute('SELECT * FROM rag_sessions WHERE owner_id='
                                  '(SELECT owner_id FROM local_installations WHERE id=1) ORDER BY created_at,id')
            for number, row in enumerate(sessions, 1):
                session = dict(row)
                session['path'] = f'rag/{number:06d}.md'
                session['turns'] = [turn for turn in rows(db, 'rag_turns', 'session_id', session['id'], 'created_at,id')
                                    if turn['owner_id'] == session['owner_id']]
                body = ['# ' + heading(session['title']), '',
                        '以下为生成时的问答记录，引用资料可能已修改或删除。', '']
                for turn in session['turns']:
                    if turn['status'] in ('pending', 'running'):
                        turn['status'] = 'unknown'
                    body += ['## 问题', '', literal(turn['question']),
                             '状态：' + heading(turn['status']), '', '### 回答记录', '', literal(turn['result_json']),
                             '### 引用元数据', '', literal(turn['evidence_json'])]
                write_text(output_root, session['path'], '\n'.join(body) + '\n')
                index.append(f'- [问答：{heading(session["title"])}]({session["path"]})')
                metadata['rag_sessions'].append(session)
        sources = db.execute('SELECT * FROM sources WHERE deleted_at IS NULL '
                             'AND owner_id=(SELECT owner_id FROM local_installations WHERE id=1) '
                             'ORDER BY created_at,id').fetchall()
        for number, row in enumerate(sources, 1):
            source = dict(row)
            identifier = f'{number:06d}'
            source['path'] = f'sources/{identifier}.md'
            source['domain'] = next((item for item in rows(db, 'domains', 'id', source['domain_id'])
                                     if item['owner_id'] == source['owner_id']), None)
            source['projects'] = [dict(project) for project in db.execute(
                'SELECT projects.* FROM projects JOIN source_projects ON projects.id=source_projects.project_id '
                'WHERE source_projects.source_id=? AND projects.owner_id=? ORDER BY projects.id',
                (source['id'], source['owner_id']))]
            source['tags'] = [item['name'] for item in rows(db, 'source_tags', 'source_id', source['id'], 'name')]
            source['legacy_rule_tags'] = source['tags']
            source['tag_suggestions'] = [item for item in rows(db, 'tag_suggestions', 'source_id', source['id'], 'created_at,id')
                                         if item['owner_id'] == source['owner_id']] if 'tag_suggestions' in tables else []
            source['tag_rejections'] = rows(db, 'tag_rejections', 'source_id', source['id']) if 'tag_rejections' in tables else []
            source['tag_assignments'] = [dict(item) for item in db.execute(
                'SELECT tag_assignments.* FROM tag_assignments JOIN knowledge_tags ON knowledge_tags.id=tag_assignments.tag_id '
                'WHERE tag_assignments.source_id=? AND knowledge_tags.owner_id=? ORDER BY tag_assignments.tag_id,tag_assignments.origin',
                (source['id'], source['owner_id']))] if 'tag_assignments' in tables else []
            source['personal_context'] = next(iter(rows(db, 'personal_context', 'source_id', source['id'])), None)
            source['experience_records'] = [item for item in rows(db, 'experience_records', 'source_id', source['id'], 'verified_at,id')
                                             if item['owner_id'] == source['owner_id']]
            source['ai_artifacts'] = [item for item in rows(db, 'ai_artifacts', 'source_id', source['id'], 'created_at,id')
                                      if item['owner_id'] == source['owner_id']] if 'ai_artifacts' in tables else []
            source['revisions'] = rows(db, 'source_revisions', 'source_id', source['id'], 'version,id')
            body = ['# ' + heading(source['title']), '', '## 原始输入', '', literal(source['raw_input'])]
            current = next((revision for revision in source['revisions']
                            if revision['id'] == source['current_revision_id']), None)
            if current:
                body += ['## 当前正文', '', literal(current['extracted_text'])]
            for label, key in [('原始网址', 'raw_url'), ('规范网址', 'canonical_url')]:
                if source[key]:
                    body += ['## ' + label, '', literal(source[key])]
            body += ['## 历史版本', '']
            for position, revision in enumerate(source['revisions'], 1):
                revision['path'] = f'versions/{identifier}-v{position:06d}.md'
                revision['evidence'] = rows(db, 'evidence_chunks', 'revision_id', revision['id'], 'ordinal,id')
                body.append(f'- [版本 {heading(revision["version"])}](../{revision["path"]})')
                write_text(output_root, revision['path'], '# ' + heading(source['title']) + '\n\n' +
                           '版本：' + heading(revision['version']) + '\n\n' + literal(revision['extracted_text']))
            source['file_asset'] = next(iter(rows(db, 'file_assets', 'source_id', source['id'])), None)
            source['images'] = rows(db, 'source_images', 'source_id', source['id'], 'revision_id,ordinal,id')
            attachments = []
            if source['file_asset']:
                asset = source['file_asset']
                asset['path'] = f'assets/{identifier}-original' + safe_suffix(asset['filename'], DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS)
                copy_asset(snapshot_root / 'assets' / asset['id'], output_root / asset['path'])
                attachments.append(f'- [原件](../{asset["path"]})')
            for position, image in enumerate(source['images'], 1):
                # Download diagnostics may contain transient paths or credentials.
                image['failure_reason'] = ''
                image['path'] = None
                if image['status'] == 'saved':
                    image['path'] = f'assets/{identifier}-image-{position:06d}' + image_suffix(image)
                    copy_asset(snapshot_root / 'assets' / 'images' / image['file_key'], output_root / image['path'])
                    attachments.append(f'- [图片 {position}](../{image["path"]})')
            if attachments:
                body += ['', '## 原件与图片', '', *attachments]
            if source['personal_context']:
                body += ['', '## 用户补充', '']
                for label, key in [('原因', 'reason'), ('备注', 'notes'), ('适用条件', 'applicability')]:
                    body += ['### ' + label, '', literal(source['personal_context'][key])]
            if source['experience_records']:
                body += ['', '## 使用记录', '']
                for experience in source['experience_records']:
                    body += [literal(experience['result']), literal(experience['notes'])]
            if source['tag_assignments']:
                body += ['', '## 正式标签', '']
                names = {tag['id']: tag['name'] for tag in metadata['knowledge_tags']}
                for assignment in source['tag_assignments']:
                    origin = '人工' if assignment['origin'] == 'manual' else 'AI'
                    stale = assignment['origin'] == 'ai' and assignment['revision_id'] != source['current_revision_id']
                    body += [literal(names[assignment['tag_id']] + ' · ' + origin + (' · 旧版本依据' if stale else ''))]
            if source['ai_artifacts']:
                body += ['', '## AI 分析记录', '', '以下结果基于生成时的版本，可能已过期。', '']
                for artifact in source['ai_artifacts']:
                    result = json.loads(artifact['result_json'])
                    snapshot = json.loads(artifact['snapshot_json'])
                    phase = snapshot.get('phase', 'single')
                    label = ('分段总结（非全文）' if phase == 'segment' else
                             '已解析证据汇总' if phase == 'synthesis' else '单次分析')
                    body += ['### ' + label + ' · ' + heading(artifact['created_at']), '']
                    plan = snapshot.get('analysis_plan')
                    if plan:
                        body += [literal('证据覆盖：' + plan['coverage'] + '；原资料采集范围：' + plan['source_coverage'])]
                    if result.get('warnings'):
                        body += [literal(warning) for warning in result['warnings']]
                    if result.get('overview'):
                        body += ['#### 一句话概述', literal(result['overview']['text'])]
                    body += ['#### 文档摘要', literal(result['summary'])]
                    for label, key in (('关键要点', 'insights'), ('适用条件与限制', 'limitations')):
                        if result.get(key):
                            body += ['#### ' + label]
                            body += [literal(item['text']) for item in result[key]]
                    candidates = [item for item in source['tag_suggestions'] if item['artifact_id'] == artifact['id']]
                    if candidates:
                        body += ['#### AI 标签候选（确认状态以记录为准）']
                        body += [literal(item['name'] + ' · ' + item['status']) for item in candidates]
                    body += [literal(artifact['result_json'])]
            write_text(output_root, source['path'], '\n'.join(body) + '\n')
            index.append(f'- [{heading(source["title"])}]({source["path"]})')
            metadata['sources'].append(source)
    write_text(output_root, 'index.md', '\n'.join(index) + '\n')
    write_text(output_root, 'metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def write_text(root: Path, name: str, text: str):
    data = text.encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('可读导出超过当前 8 GiB 验证上限')
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as target:
        get_platform().files.protect(path)
        target.write(data)


def copy_asset(original: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(original, target)
    get_platform().files.protect(target)


def readable_snapshot(paths: LocalPaths, root: Path):
    """Copy eligible originals while the stopped-library exclusive lock is held."""
    database = root / 'knowledge.db'
    with data_lock(paths, exclusive=True, prepare=False):
        if paths.database.is_symlink() or not paths.database.is_file():
            raise ValueError('本地知识库不存在或路径不安全')
        snapshot(paths.database, database)
        check_database(database)
        with closing(sqlite3.connect(database)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            eligible = ('SELECT id FROM sources WHERE deleted_at IS NULL '
                        'AND owner_id=(SELECT owner_id FROM local_installations WHERE id=1)')
            # Only the temporary snapshot is filtered. Historical source content
            # and the original database remain untouched.
            for table in ('file_assets', 'source_images'):
                db.execute(f'DELETE FROM {table} WHERE source_id NOT IN ({eligible})')
            db.commit()
        sanitize(database)
        check_database(database)
        originals = asset_records(database)
        if sum(size for size, _ in originals.values()) > MAX_BYTES:
            raise ValueError('可读导出超过当前 8 GiB 验证上限')
        for name, (size, digest) in originals.items():
            original = paths.data / name
            components = [paths.data.joinpath(*Path(name).parts[:index])
                          for index in range(1, len(Path(name).parts) + 1)]
            if any(component.is_symlink() for component in components) or not original.is_file():
                raise ValueError('待导出资料的原件缺失或路径不安全')
            if original.stat().st_size != size:
                raise ValueError('待导出资料的原件大小不一致')
            copy_asset(original, root / name)
            if file_digest(root / name) != digest:
                raise ValueError('待导出资料的原件摘要不一致')
        check_assets(database, root)


def verify_readable(package: Path) -> dict:
    """Validate the emitted ZIP without interpreting or extracting user contents."""
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or len(names) > 50000:
            raise ValueError('可读导出包含重复文件或文件过多')
        for entry in entries:
            mode = (entry.external_attr >> 16) & 0o170000
            if entry.is_dir() or mode not in (0, stat.S_IFREG) or entry.flag_bits & 1:
                raise ValueError('可读导出包含链接、特殊文件或加密项')
        if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > MAX_MANIFEST:
            raise ValueError('可读导出清单缺失或过大')
        manifest = json.loads(archive.read('manifest.json'))
        if (not isinstance(manifest, dict) or manifest.get('format') != FORMAT or
                type(manifest.get('version')) is not int or manifest['version'] != 1 or
                not isinstance(manifest.get('files'), list)):
            raise ValueError('可读导出格式或版本不受支持')
        expected = {}
        for record in manifest['files']:
            if not isinstance(record, dict):
                raise ValueError('可读导出文件记录无效')
            name, size, digest = record.get('path'), record.get('size_bytes'), record.get('sha256')
            if (not isinstance(name, str) or not FILE_PATH.fullmatch(name) or name in expected or
                    type(size) is not int or size < 0 or not isinstance(digest, str) or
                    not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('可读导出路径、大小或摘要记录无效')
            expected[name] = (size, digest)
        if not {'index.md', 'metadata.json'} <= set(expected) or set(names) != set(expected) | {'manifest.json'}:
            raise ValueError('可读导出文件与清单不一致')
        if sum(size for size, _ in expected.values()) > MAX_BYTES:
            raise ValueError('可读导出超过当前 8 GiB 验证上限')
        for name, (size, digest) in expected.items():
            if archive.getinfo(name).file_size != size:
                raise ValueError('可读导出文件大小不一致')
            actual = hashlib.sha256()
            total = 0
            with archive.open(name) as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    total += len(block)
                    if total > size:
                        raise ValueError('可读导出数据超过清单大小')
                    actual.update(block)
            if total != size or actual.hexdigest() != digest:
                raise ValueError('可读导出文件摘要不一致')
    return {'format': FORMAT, 'version': 1, 'files': len(expected)}


def export_readable(paths: LocalPaths, output: Path) -> dict:
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(paths.data.resolve()):
        raise ValueError('导出位置不能覆盖已有文件或位于知识库内')
    if not output.parent.is_dir():
        raise ValueError('导出目录不存在')
    with tempfile.TemporaryDirectory(prefix='.pka-readable-', dir=output.parent) as temporary:
        root = Path(temporary)
        snapshot_root = root / 'snapshot'
        snapshot_root.mkdir(mode=0o700)
        readable_snapshot(paths, snapshot_root)
        readable = root / 'readable'
        readable.mkdir(mode=0o700)
        metadata = render_library(snapshot_root / 'knowledge.db', snapshot_root, readable)
        files = sorted(path for path in readable.rglob('*') if path.is_file())
        records = [{'path': path.relative_to(readable).as_posix(), 'size_bytes': path.stat().st_size,
                    'sha256': file_digest(path)} for path in files]
        if sum(record['size_bytes'] for record in records) > MAX_BYTES:
            raise ValueError('可读导出超过当前 8 GiB 验证上限')
        manifest = {'format': FORMAT, 'version': 1, 'files': records}
        encoded = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
        if len(encoded) > MAX_MANIFEST:
            raise ValueError('可读导出清单过大')
        staged = root / 'readable.zip'
        with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', encoded)
            for record in records:
                archive.write(readable / record['path'], record['path'])
        get_platform().files.protect(staged)
        result = verify_readable(staged)
        get_platform().files.publish_file(staged, output)
        result['sources'] = len(metadata['sources'])
        return result
