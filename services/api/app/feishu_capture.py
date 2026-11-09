"""Shared Feishu document/version/media rules; native capabilities are injected."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from PIL import Image

from .schemas import FeishuDocumentMetadata, FeishuImageMetadata

HOSTS = ('feishu.cn', 'larksuite.com', 'larkoffice.com')
MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ExportDenied(ValueError):
    pass


def is_feishu(url):
    host = (urlsplit(url).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain) for domain in HOSTS)


def checked_link(value):
    value = value.strip()
    if len(value) > 8192:
        raise ValueError('链接超过长度限制')
    parts = urlsplit(value)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('链接格式无效，请重新加入')
    if is_feishu(value) and (parts.scheme != 'https' or parts.port not in {None, 443}
            or not re.fullmatch(r'/(wiki|docx|doc)/[A-Za-z0-9]+/?', parts.path)):
        raise ValueError('当前支持飞书 /wiki/、/docx/ 和 /doc/ 文档链接；其他类型暂未接入')
    return value


def command_error(value):
    error = value.get('error') or {}
    kind = error.get('type')
    if kind == 'authorization':
        if error.get('subtype') == 'permission_denied' and 'export permission' in error.get('message', ''):
            return ExportDenied('当前飞书账号没有图片导出权限')
        return ValueError('飞书授权不足，请在设置中补充文档和图片读取授权')
    return ValueError({'authentication': '飞书登录已失效，请在设置中重新连接',
        'auth': '飞书登录已失效，请在设置中重新连接',
        'config': '飞书CLI尚未完成应用配置，请先完成配置',
        'permission': '缺少文档读取权限，请检查文档共享设置',
        'network': '连接飞书失败，请检查网络'}.get(kind, '飞书CLI操作失败，请检查登录、文档权限和链接类型'))


def cli_json(external, executable, profile, arguments, timeout=120, directory=None):
    selected = profile.strip()
    arguments = (['--profile', selected] if selected else []) + arguments
    result = external.run_cli(executable, arguments, timeout, directory,
                              MAX_IMAGE_BYTES if directory else None)
    if result.exit_code:
        try:
            value = json.loads(result.errors)
        except (ValueError, TypeError):
            value = {}
        raise command_error(value)
    try:
        value = json.loads(result.output)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('飞书CLI返回格式无效，请检查CLI版本（已验证1.0.97）') from None
    if value.get('ok') is False:
        raise command_error(value)
    return value


def parse_document(value, version):
    document = (value.get('data') or {}).get('document') or {}
    text, identifier, revision = (document.get(key) for key in ('content', 'document_id', 'revision_id'))
    if (value.get('ok') is not True or value.get('identity') != 'user' or not isinstance(text, str)
            or not text.strip() or not isinstance(identifier, str) or not 1 <= len(identifier) <= 200
            or type(revision) is not int or revision < 0):
        raise ValueError('飞书未返回有效正文及版本信息，无法保存')
    if len(text) > 1_000_000:
        raise ValueError('飞书文档超过100万字符，正文未被截断')
    title = re.search(r'<title>(.*?)</title>', text, re.S) or re.search(r'^# (.+)$', text, re.M)
    return {'markdown': text, 'title': (title.group(1).strip() if title else '飞书文档')[:500],
            'document_id': identifier, 'revision_id': revision, 'cli_version': version[:80],
            'partial': bool(str(document.get('tips') or '').strip() or (value.get('data') or {}).get('warnings')),
            'images': []}


def image_references(xml):
    tags = re.findall(r'<img\b[^>]*>', xml, re.I)
    if len(tags) > 200:
        raise ValueError('文档超过200张图片，当前无法完整导入')
    values = []
    for ordinal, tag in enumerate(tags):
        attributes = {}
        for name, _, text in re.findall(r'([A-Za-z_][\w:-]*)\s*=\s*([\"\'])(.*?)\2', tag, re.S):
            for old, new in (('&quot;', '"'), ('&apos;', "'"), ('&lt;', '<'), ('&gt;', '>'), ('&amp;', '&')):
                text = text.replace(old, new)
            attributes[name.lower()] = text
        values.append({'id': str(uuid4()), 'ordinal': ordinal,
            'token': attributes.get('token') or attributes.get('file_token') or attributes.get('src') or '',
            'block_id': attributes.get('id', '')[:200],
            'caption': (attributes.get('alt') or attributes.get('name') or f'图片 {ordinal + 1}')[:500],
            'ocr_text': '', 'failure_reason': '', 'retrieval_method': 'download', 'processed': False})
    return values


def legacy_document(value, client_files):
    """Translate old Codable documents once; retain body, versions and media IDs."""
    from .capture_handoff import legacy_asset
    metadata = FeishuDocumentMetadata(document_id=value['documentID'], revision_id=value['revisionID'],
                                      cli_version=value['cliVersion']).model_dump()
    if not isinstance(value['markdown'], str) or not 0 < len(value['markdown']) <= 1_000_000:
        raise ValueError('旧飞书正文大小无效')
    images, assets = [], {}
    for image in value.get('images', []):
        media = FeishuImageMetadata(id=image['id'], ordinal=image['ordinal'], block_id=image['blockID'],
            caption=image['caption'], status='pending' if image.get('file') else 'unavailable',
            failure_reason=image.get('failureReason', ''), ocr_text=image.get('ocrText', ''),
            retrieval_method=image.get('retrievalMethod', 'download')).model_dump(mode='json')
        media.update(token=image.get('token', ''), processed=True)
        images.append(media)
        if image.get('file'):
            assets[media['id']] = legacy_asset(image['file'], client_files)
    if len(images) > 200 or len({i['id'] for i in images}) != len(images) or len({i['ordinal'] for i in images}) != len(images):
        raise ValueError('旧飞书图片清单无效')
    return {**metadata, 'markdown': value['markdown'], 'title': value['title'][:500],
            'partial': bool(value['partial']), 'images': images}, assets


def fetch_document(link, options, external):
    if not is_feishu(link):
        raise ValueError('这不是受支持的飞书文档链接')
    parts = urlsplit(checked_link(link))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
    executable = external.resolve_cli(options['executable'])
    profile = options['profile']
    version_result = external.run_cli(executable, (['--profile', profile.strip()] if profile.strip() else [])
                                      + ['--version'], 15, None, None)
    if version_result.exit_code:
        raise ValueError('无法读取飞书CLI版本，请检查所选程序')
    version = version_result.output.decode(errors='replace').strip()[:80] or 'unknown'
    document = parse_document(cli_json(external, executable, profile, ['docs', '+fetch', '--doc', url,
        '--scope', 'full', '--doc-format', 'markdown', '--as', 'user', '--format', 'json']), version)
    result = cli_json(external, executable, profile, ['docs', '+fetch', '--doc', url, '--scope', 'full',
        '--doc-format', 'xml', '--detail', 'full', '--revision-id', str(document['revision_id']),
        '--as', 'user', '--format', 'json'])
    original = (result.get('data') or {}).get('document') or {}
    if (original.get('document_id') != document['document_id']
            or type(original.get('revision_id')) is not int
            or original.get('revision_id') != document['revision_id'] or not isinstance(original.get('content'), str)):
        raise ValueError('图片结构与正文版本不一致，请重试导入')
    document['images'] = image_references(original['content'])
    return document


def archive_images(document, options, external, store, identifier, progress):
    executable = external.resolve_cli(options['executable']) if any(not i['processed'] for i in document['images']) else None
    for image in document['images']:
        if image['processed']:
            continue
        progress(f"正在获取飞书图片 {image['ordinal'] + 1} / {len(document['images'])}…")
        directory = store.work(identifier) / 'downloads' / image['id']
        try:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', image['token']):
                raise ValueError('图片未提供可下载的资源标识')
            store.files.directory(directory)
            def retrieve(command):
                return cli_json(external, executable, options['profile'], ['docs', command, '--token', image['token'],
                    '--output', 'image', '--as', 'user', '--format', 'json'], timeout=60, directory=directory)
            try:
                result = retrieve('+media-download')
            except ExportDenied:
                shutil.rmtree(directory)
                store.files.directory(directory)
                result = retrieve('+media-preview')
                image['retrieval_method'] = 'preview'
            path = (result.get('data') or result).get('saved_path')
            if not isinstance(path, str):
                raise ValueError('CLI未返回图片文件')
            original = Path(path) if Path(path).is_absolute() else directory / path
            if original.parent != directory or '..' in original.parts:
                raise ValueError('CLI图片保存路径无效')
            # The CLI creates private output; enforce permissions before bounded read.
            store.files.protect(original)
            raw = store.files.read(original, maximum=MAX_IMAGE_BYTES, parents=True)
            if not raw:
                raise ValueError('图片内容为空')
            total = sum(i.get('asset', {}).get('size', 0) for i in document['images'])
            if total + len(raw) > MAX_DOCUMENT_BYTES:
                raise ValueError('整篇图片超过200MB限制')
            with Image.open(BytesIO(raw)) as verified:
                if verified.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'} or verified.width * verified.height > 30_000_000:
                    raise ValueError('图片格式或像素大小无效')
                verified.verify()
            try:
                text = external.recognize_image(original)
            except Exception:
                text = ''  # A valid original must survive optional OCR failure.
            key = str(uuid4())
            store.files.atomic_write(store.work(identifier) / key, raw)
            image.update(asset={'key': key, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}, ocr_text=text)
        except OSError:
            raise  # Storage failure must not silently become a partial import.
        except Exception:
            image['failure_reason'] = '图片未能获取，请检查文档导出权限、图片格式或大小限制'
        finally:
            if directory.exists():
                shutil.rmtree(directory)
        image['processed'] = True
        store.checkpoint(identifier, document)
    return document


def capture_payload(document, submission):
    text = document['markdown']
    metadata = []
    for image in document['images']:
        token = re.escape(image.get('token', ''))
        if token:
            destination = r'(?:https://(?:[A-Za-z0-9-]+\.)*(?:feishu\.cn|larksuite\.com|larkoffice\.com)/file/)?' + token + r'(?:\?[^)\s]*)?'
            pattern = r'<img\b[^>]*\b(?:src|token|file_token)\s*=\s*[\"\']' + destination + r'[\"\'][^>]*>|!\[[^\]]*\]\(' + destination + r'(?:\s+"[^"]*")?\)'
            text = re.sub(pattern, f"![图片 {image['ordinal'] + 1}](know-image://{image['id']})", text, count=1, flags=re.I)
        metadata.append(FeishuImageMetadata(**{key: image[key] for key in
            ('id', 'ordinal', 'block_id', 'caption', 'failure_reason', 'ocr_text', 'retrieval_method')},
            status='unavailable' if image['failure_reason'] else 'pending').model_dump(mode='json'))
    context = {'domain_id': submission['domain_id'], 'project_id': submission['project_id']}
    return {'capture_type': 'feishu_document', 'title': document['title'], 'source_url': submission['text'].strip(),
        'content': {'text': text}, 'coverage': 'partial_text' if document['partial'] or any(i['failure_reason'] for i in document['images']) else 'full_text',
        'context': context, 'client': {'kind': 'desktop', 'version': '0.1.0'},
        'feishu_document': {key: document[key] for key in ('document_id', 'revision_id', 'cli_version')}, 'feishu_images': metadata}
