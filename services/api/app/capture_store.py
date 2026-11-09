"""Private, device-only capture journal. SQLite remains the knowledge authority."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
from uuid import UUID, uuid4

from .errors import ApiError
from .capture_contract import CaptureSubmission
from .platform_support import get_platform
from .platform_support.contracts import UnsafeFile

MAX_RECORD = 32 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(identifier):
    try:
        value = str(UUID(identifier))
        if value != identifier.lower():
            raise ValueError()
        return value
    except (ValueError, AttributeError, TypeError):
        raise ApiError(422, 'invalid_capture_id', '采集编号无效') from None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


class CaptureStore:
    def __init__(self, data):
        self.data = Path(data)
        self.root = self.data / 'jobs/capture-queue-v1'
        self.client_files = self.data / 'capture-queue/files'
        self.files = get_platform().files

    @contextmanager
    def locked(self):
        with ExitStack() as stack:
            deadline = time.monotonic() + 15
            while True:
                try:
                    stack.enter_context(self.files.lock(self.root / '.queue.lock', exclusive=True))
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ApiError(409, 'capture_queue_busy', '采集队列正在更新，请稍后刷新') from None
                    time.sleep(0.02)
            yield

    def path(self, identifier):
        return self.root / (canonical(identifier) + '.json')

    def work(self, identifier):
        return self.root / canonical(identifier)

    def load(self, identifier, owner=None):
        try:
            raw = self.files.read(self.path(identifier), maximum=MAX_RECORD, parents=True)
            value = json.loads(raw) if raw else None
            if value is not None and (not isinstance(value, dict) or value.get('version') != 1
                    or canonical(value.get('id')) != canonical(identifier)
                    or value.get('state') not in {'queued', 'running', 'completed', 'failed'}
                    or not isinstance(value.get('input'), dict)):
                raise ValueError()
            if value is not None:
                if (not all(isinstance(value.get(key), str) for key in
                        ('owner_id', 'fingerprint', 'title', 'created_at', 'updated_at', 'message'))
                        or not isinstance(value.get('assets'), dict) or len(value['fingerprint']) != 64
                        or CaptureSubmission.model_validate(value['input']).id != value['id']):
                    raise ValueError()
        except (OSError, ValueError, TypeError):
            raise ApiError(409, 'capture_journal_invalid', '采集执行记录不可用，原文件已保留，请检查队列') from None
        if owner is not None and (value is None or value.get('owner_id') != owner):
            raise ApiError(404, 'not_found', '采集任务不存在')
        return value

    def save(self, value):
        value['updated_at'] = now()
        raw = json.dumps(value, ensure_ascii=False).encode()
        if len(raw) > MAX_RECORD:
            raise ValueError('采集执行记录超过大小限制')
        self.files.atomic_write(self.path(value['id']), raw)

    def checkpoint(self, identifier, value=None):
        path = self.work(identifier) / 'document.json'
        if value is not None:
            raw = json.dumps(value, ensure_ascii=False).encode()
            if len(raw) > MAX_RECORD:
                raise ValueError('飞书检查点超过大小限制')
            self.files.atomic_write(path, raw)
            return value
        try:
            raw = self.files.read(path, maximum=MAX_RECORD, parents=True)
            return json.loads(raw) if raw else None
        except (ValueError, TypeError):
            raise ValueError('采集检查点损坏，原文件和原件已保留') from None

    def entries(self, owner=None):
        if not self.root.exists():
            return []
        try:
            self.files.safe_path(self.root / '.read-check')
        except ValueError:
            raise ApiError(409, 'capture_queue_unsafe', '采集执行目录不安全，原文件已保留') from None
        values = []
        self.warnings = []
        for path in self.root.glob('*.json'):
            try:
                value = self.load(path.stem)
            except (ApiError, OSError):
                self.warnings = ['部分采集执行记录不可用，已保留原文件；其他资料可继续使用']
                continue
            if value is not None and (owner is None or value['owner_id'] == owner):
                values.append(value)
        return sorted(values, key=lambda item: (item['created_at'], item['id']))

    def receive_asset(self, identifier, asset, limit):
        path = self.client_files / asset['key']
        # safe_path/read refuse symlink parents, special files and another uid.
        try:
            data = self.files.read(path, maximum=limit, parents=True)
        except UnsafeFile:
            raise ApiError(409, 'capture_original_unsafe', '采集暂存原件路径或权限不安全，原队列已保留') from None
        if not data:
            raise ApiError(409, 'capture_original_missing', '采集暂存原件缺失或为空，原队列已保留')
        digest = hashlib.sha256(data).hexdigest()
        if asset.get('sha256') and asset['sha256'] != digest:
            raise ApiError(409, 'capture_original_changed', '采集暂存原件已变更，原队列已保留')
        key = str(uuid4())
        self.files.atomic_write(self.work(identifier) / key, data)
        return {'key': key, 'sha256': digest, 'size': len(data)}

    def original(self, identifier, asset, limit):
        if canonical(asset['key']) != asset['key']:
            raise ValueError('内核原件标识无效')
        path = self.work(identifier) / asset['key']
        data = self.files.read(path, maximum=limit, parents=True)
        if data is None or len(data) != asset['size'] or hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('内核采集原件缺失或已变更')
        return data

    def update(self, identifier, **changes):
        with self.locked():
            value = self.load(identifier)
            value.update(changes)
            self.save(value)
            return value

    def clean(self, identifier):
        directory = self.work(identifier)
        if directory.exists():
            self.files.safe_path(directory / '.cleanup-check')
            shutil.rmtree(directory)

    def recover(self):
        with self.locked():
            for value in self.entries():
                if value['state'] == 'running':
                    value.update(state='queued', message='等待后台继续归档')
                    self.save(value)

    def claim(self):
        with self.locked():
            for value in self.entries():
                if value['state'] == 'queued' and not value.get('hidden'):
                    value.update(state='running', message='正在接收资料…')
                    self.save(value)
                    return value
        return None

    def purge_source(self, owner, source_id, request_ids):
        """Permanent knowledge deletion also clears its device execution copies."""
        with self.locked():
            for value in self.entries(owner):
                if value.get('source_id') != source_id and value['id'] not in request_ids:
                    continue
                value['input'].update(text='', title='已删除资料', file=None, legacy_sent=True,
                    legacy_source_id=source_id, feishu={'executable': '', 'profile': ''})
                value.update(state='completed', source_id=source_id, title='已删除资料', assets={},
                             message='原资料已永久删除，不会重新采集')
                self.save(value)
                self.clean(value['id'])
