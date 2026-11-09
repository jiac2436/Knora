"""Non-secret startup compatibility metadata shared by desktop and health API."""
from functools import lru_cache
import hashlib
from pathlib import Path
import platform

from .migrations import CURRENT_VERSION
from .platform_support import get_platform

KERNEL_VERSION = '0.1.0'
CONTRACT_VERSION = 1


@lru_cache(maxsize=1)
def runtime_id(root=None):
    manifest = (root or Path(__file__).resolve().parents[2]) / 'manifest.json'
    if not manifest.is_file():
        return 'development'
    with manifest.open('rb') as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('运行包清单超过大小限制')
    return hashlib.sha256(raw).hexdigest()


def metadata():
    adapter = get_platform()
    return {'kernel_version': KERNEL_VERSION, 'contract_version': CONTRACT_VERSION,
            'schema_version': CURRENT_VERSION, 'platform': adapter.name,
            'architecture': platform.machine(), 'runtime_id': runtime_id(),
            'capabilities': adapter.capabilities}


def validate_client(contract_version, schema_version):
    if contract_version != CONTRACT_VERSION or schema_version != CURRENT_VERSION:
        raise ValueError('客户端与内核版本不兼容，请使用同一安装包重新启动')
