"""Download pinned public wheels/models and reproduce a verified extension tree.

No dependency resolution, pip, compilation, shell, or system Python is used.
The signed app supplies both input digests and the exact accepted output manifest.
"""
from __future__ import annotations

import base64
import csv
import hashlib
from http.client import HTTPException
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import zipfile

from .runtime_extensions import (active, catalog, digest, package_directory, package_lock,
                                 read_json, safe_directory, verify_package)

HOSTS = {'pypi.tuna.tsinghua.edu.cn', 'mirrors.tuna.tsinghua.edu.cn', 'mirrors.aliyun.com',
         'mirrors.ustc.edu.cn', 'paddle-model-ecology.bj.bcebos.com',
         'modelscope.cn', 'www.modelscope.cn', 'cdn-lfs-cn-1.modelscope.cn',
         'cdn-lfs-cn-2.modelscope.cn', 'cdn-lfs-cn-3.modelscope.cn'}


def component_list(item):
    titles = {'paddleocr': '文字识别引擎', 'paddlepaddle': '模型推理引擎',
              'opencv-contrib-python': '图像处理组件', 'paddlex': '识别流程组件', 'numpy': '数值计算组件'}
    return [dict(id=artifact['sha256'], title=titles.get(artifact.get('title'), artifact.get('title') or
        artifact.get('filename', artifact.get('path', '组件')).split('-')[0]),
        detail=' · '.join(value for value in [artifact.get('title', ''), artifact.get('version', '')] if value),
        bytes=artifact['bytes']) for artifact in item.get('dependency_download', {}).get('artifacts', [])]


def checked_url(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.hostname not in HOSTS or parts.username or parts.password
            or parts.port not in (None, 443) or parts.fragment):
        raise ValueError('依赖下载地址不在已验证的来源中')
    return value


class VerifiedRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_artifact(item, destination, progress, *, received=0, total_bytes=1, item_progress=None):
    import certifi
    opener = build_opener(VerifiedRedirect(), HTTPSHandler(
        context=ssl.create_default_context(cafile=certifi.where())))
    for url in item['urls']:
        checked_url(url)
        try:
            deadline = time.monotonic() + 1800
            request = Request(url, headers={'Accept-Encoding': 'identity', 'User-Agent': 'KnowledgeInbox/1.0'})
            checksum, total = hashlib.sha256(), 0
            with opener.open(request, timeout=30) as response, destination.open('wb') as stream:
                checked_url(response.url)
                if response.status != 200:
                    raise ValueError('下载源未返回完整文件')
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > item['bytes'] or time.monotonic() > deadline:
                        raise ValueError('依赖文件超过大小或时间限制')
                    checksum.update(chunk)
                    stream.write(chunk)
                    progress('downloading', min(0.85, (received + total) / max(1, total_bytes) * 0.85))
                    if item_progress:
                        item_progress(total / max(1, item['bytes']))
            if total != item['bytes'] or checksum.hexdigest() != item['sha256']:
                raise ValueError('依赖文件校验失败')
            return
        except InterruptedError:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, HTTPException):
            destination.unlink(missing_ok=True)
    raise ValueError('已验证的下载源暂时不可用，请稍后重试或导入离线扩展包')


def _name(value):
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in value or '\x00' in value
            or str(path) != value or len(path.parts) > 30):
        raise ValueError('依赖安装路径不安全')
    return path


def _distribution_record(name):
    parts = PurePosixPath(name).parts
    return len(parts) == 2 and parts[0].endswith('.dist-info') and parts[1] == 'RECORD'


def materialize(manifest_file, artifacts, destination, progress=lambda *_: None, *, compatibility=None):
    """Reconstruct the already approved output, including normalized RECORDs."""
    manifest = read_json(manifest_file)
    entries = {entry['path']: entry for entry in manifest['files']}
    if len(entries) != len(manifest['files']) or len(entries) > 50_000:
        raise ValueError('扩展文件清单无效')
    for path in entries:
        _name(path)
    destination.mkdir(mode=0o700)
    compatibility = compatibility or {}

    def input_size(path):
        return compatibility[path]['source_bytes'] if path in compatibility else entries[path]['bytes']

    def write(path, data):
        expected = entries.get(path)
        if expected is not None and path in compatibility:
            from .dependency_native import compatible_bytes
            data = compatible_bytes(data, compatibility[path], expected)
        if expected is None or len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise ValueError('依赖内容与已验收扩展不一致：' + path)
        target = destination.joinpath(*_name(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if digest(target) != expected['sha256']:
                raise ValueError('依赖文件冲突')
            return
        target.write_bytes(data)
        target.chmod(expected['mode'])

    records = []
    for item, file in artifacts:
        progress('installing', 0.87)
        if file.stat().st_size != item['bytes'] or digest(file) != item['sha256']:
            raise ValueError('下载依赖校验失败，未安装')
        if item['kind'] == 'file':
            write(item['path'], file.read_bytes())
            continue
        if item['kind'] == 'tar':
            wanted = {str(_name(member['member'])): member['path'] for member in item['members']}
            if len(wanted) != len(item['members']) or any(path not in entries for path in wanted.values()):
                raise ValueError('归档组件清单无效')
            seen = set()
            with tarfile.open(file) as archive:
                for member in archive:
                    name = str(_name(str(PurePosixPath(member.name))))
                    if name not in wanted:
                        continue
                    path = wanted[name]
                    if name in seen or not member.isfile() or member.size != input_size(path):
                        raise ValueError('归档组件类型或大小不匹配')
                    seen.add(name)
                    with archive.extractfile(member) as stream:
                        write(path, stream.read(input_size(path) + 1))
                    progress('installing', 0.9)
            if seen != set(wanted):
                raise ValueError('归档组件不完整')
            continue
        if item['kind'] != 'wheel':
            raise ValueError('不支持的依赖格式')
        with zipfile.ZipFile(file) as archive:
            seen, members = set(), set()
            if len(archive.infolist()) > 50_000:
                raise ValueError('依赖文件数量超过限制')
            for entry in archive.infolist():
                path = _name(entry.filename.rstrip('/'))
                if entry.filename in seen:
                    raise ValueError('依赖存在重复文件')
                seen.add(entry.filename)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or (not entry.is_dir() and mode == stat.S_IFDIR):
                    raise ValueError('依赖包含特殊文件')
                if entry.is_dir():
                    continue
                relative = str(path)
                if len(path.parts) > 2 and path.parts[0].endswith('.data') and path.parts[1] in {'purelib', 'platlib', 'data'}:
                    relative = str(PurePosixPath(*path.parts[2:]))
                # Only accepted output paths are used; entry-point scripts are
                # omitted and installation hooks are never executed.
                target = 'packages/' + relative
                if target not in entries:
                    continue
                members.add(relative)
                if relative in item.get('replaced_files', []):
                    continue
                if _distribution_record(relative):
                    records.append((relative, members))
                    continue
                if entry.file_size != input_size(target):
                    raise ValueError('依赖文件长度不一致')
                with archive.open(entry) as stream:
                    data = stream.read(input_size(target) + 1)
                write(target, data)
            info = [name.rsplit('/', 1)[0] for name in members if _distribution_record(name)]
            if len(info) != 1:
                raise ValueError('依赖元数据无效')
            for name in item.get('record_additions', []):
                if 'packages/' + str(_name(name)) not in entries:
                    raise ValueError('依赖附加文件清单无效')
                members.add(name)
            for suffix, data in [('INSTALLER', b'uv'), ('REQUESTED', b'')]:
                relative = info[0] + '/' + suffix
                members.add(relative)
                write('packages/' + relative, data)
    for record, members in records:
        content = io.StringIO(newline='')
        writer = csv.writer(content, lineterminator='\n')
        for path in sorted(members):
            entry = entries['packages/' + path]
            encoded = base64.urlsafe_b64encode(bytes.fromhex(entry['sha256'])).decode().rstrip('=')
            writer.writerow([path, '', ''] if path == record else [path, 'sha256=' + encoded, str(entry['bytes'])])
        write('packages/' + record, content.getvalue().encode())
    actual = {path.relative_to(destination).as_posix() for path in destination.rglob('*') if path.is_file()}
    if actual != set(entries):
        raise ValueError('依赖未能生成完整扩展：' + ', '.join(sorted(set(entries) - actual)[:3]))
    shutil.copyfile(manifest_file, destination / 'manifest.json')


def health_check(path, key, progress):
    code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def offline(event, args):
    if event == "socket.connect": raise RuntimeError("安装自检禁止网络连接")
sys.addaudithook(offline)
if sys.argv[2] == "semantic":
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5", specific_model_path=sys.argv[3], local_files_only=True)
    vector = list(model.embed(["扩展安装合成自检"]))
    assert len(vector) == 1 and len(vector[0]) == 512
elif sys.argv[2] == "documents":
    import anydoc, pdf_inspector
    assert "42" in anydoc.to_markdown_bytes(b"name,value\\nlocal,42\\n", "csv")
else:
    import os
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = "BOS"
    from paddleocr import PaddleOCR
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    root = Path(sys.argv[3])
    engine = PaddleOCR(text_detection_model_name="PP-OCRv6_small_det", text_recognition_model_name="PP-OCRv6_small_rec",
        text_detection_model_dir=str(root/"PP-OCRv6_small_det_infer"), text_recognition_model_dir=str(root/"PP-OCRv6_small_rec_infer"),
        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device="cpu")
    image = Image.new("RGB", (800, 180), "white")
    ImageDraw.Draw(image).text((30, 50), "LOCAL OCR 2026", fill="black", font=ImageFont.load_default(size=48))
    text = []
    for value in engine.predict(np.asarray(image)):
        value = getattr(value, "json", value)
        if callable(value): value = value()
        value = value.get("res", value)
        text.extend(value["rec_texts"])
    assert "2026" in " ".join(text), text
'''
    with tempfile.TemporaryDirectory(prefix='pka-dependency-check-') as temporary:
        environment = {'PATH': '/usr/bin:/bin', 'HOME': temporary, 'TMPDIR': temporary,
                       'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
            str(path / 'packages'), key, str(path / ('ocr-models' if key == 'ocr' else 'model'))], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 120
            while process.poll() is None:
                progress('checking', 0.97)
                if time.monotonic() > deadline:
                    raise ValueError('扩展自检超时，尚未启用，请重试')
                time.sleep(0.2)
            if process.returncode != 0:
                raise ValueError('扩展自检未通过，原有资料和扩展保持不变')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def install_dependencies(runtime, root, key, progress=lambda *_: None, *, component_progress=None):
    from .extension_install import mutation, write_json, cancel_removal
    item = catalog(runtime)['packages'][key]
    plan = item.get('dependency_download')
    if not plan or key not in {'semantic', 'documents', 'ocr'} or (key == 'ocr' and not plan.get('compatibility')):
        raise ValueError('此扩展暂时仅支持离线安装')
    if plan.get('version') != 1:
        raise ValueError('依赖下载清单版本不受支持')
    manifest = runtime.joinpath(*_name(plan['manifest']).parts)
    if digest(manifest) != item['manifest_sha256']:
        raise ValueError('依赖安装清单无效')
    compatibility = {}
    if plan.get('compatibility'):
        value = plan['compatibility']
        file = runtime.joinpath(*_name(value['file']).parts)
        if digest(file) != value['sha256']:
            raise ValueError('原生兼容组件清单校验失败')
        value = read_json(file)
        if value.get('version') != 1:
            raise ValueError('原生兼容组件版本不受支持')
        compatibility = value['files']
    with mutation(root):
        destination = package_directory(root, item)
        if destination.exists():
            verify_package(destination, item)
        else:
            total = sum(artifact['bytes'] for artifact in plan['artifacts'])
            if shutil.disk_usage(root).free < total + item['installed_bytes'] + 20 * 1024 * 1024:
                raise ValueError('磁盘可用空间不足，尚未修改已安装扩展')
            staging = safe_directory(root / 'staging')
            staging.mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix='dependencies-', dir=staging) as temporary:
                temporary = Path(temporary)
                downloaded, received = [], 0
                for index, artifact in enumerate(plan['artifacts']):
                    file = temporary / f'artifact-{index}'
                    if component_progress:
                        component_progress(index, 'downloading', 0)
                    try:
                        fetch_artifact(artifact, file, progress, received=received, total_bytes=total,
                            item_progress=(lambda fraction: component_progress(index, 'downloading', fraction)) if component_progress else None)
                    except BaseException as error:
                        if component_progress:
                            component_progress(index, 'cancelled' if isinstance(error, InterruptedError) else 'failed', 0)
                        raise
                    if component_progress:
                        component_progress(index, 'verified', 1)
                    received += artifact['bytes']
                    downloaded.append((artifact, file))
                stage = temporary / 'payload'
                materialize(manifest, downloaded, stage, progress, compatibility=compatibility)
                verify_package(stage, item)
                health_check(stage, key, progress)
                verify_package(stage, item)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                safe_directory(destination.parent)
                progress('activating', 0.99)
                with package_lock(root, item, exclusive=True):
                    stage.rename(destination)
                if component_progress:
                    for index in range(len(plan['artifacts'])):
                        component_progress(index, 'completed', 1)
        selected = active(root)
        selected[key] = item['sha256']
        write_json(root / 'active.json', {'version': 1, 'packages': selected})
        cancel_removal(root, key)
        progress('completed', 1)
    return key
