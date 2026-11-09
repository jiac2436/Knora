"""Import a stopped legacy database into a new local data directory."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .database import make_engine
from .local_paths import LocalPaths
from .local_runtime import data_lock, initialize_identity, validate_credential
from .migrations import migrate, snapshot
from .models import AgentConnection, FileAsset, Session as UserSession, Source, SourceImage
from .platform_support import get_platform


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_assets(db: Session, assets: Path) -> None:
    records = [(asset.id, asset.size_bytes, asset.sha256) for asset in db.scalars(select(FileAsset))]
    records += [(f'images/{image.file_key}', image.size_bytes, image.sha256)
                for image in db.scalars(select(SourceImage).where(SourceImage.status == 'saved'))]
    for key, size, digest in records:
        path = assets / key
        if not path.resolve().is_relative_to(assets.resolve()) or path.is_symlink() or not path.is_file():
            raise ValueError('旧库原件缺失或路径不安全')
        if size is None or digest is None or path.stat().st_size != size or file_digest(path) != digest:
            raise ValueError('旧库原件大小或摘要不一致')


def _import_legacy(source: Path, source_assets: Path, destination: LocalPaths,
                  credential: str, owner_id: str, *, source_stopped: bool) -> dict:
    validate_credential(credential)
    if not source_stopped:
        raise ValueError('导入前必须停止旧 API、Worker 和 MCP 服务')
    source = source.expanduser().resolve(strict=True)
    source_assets = source_assets.expanduser().resolve(strict=True)
    if not source.is_file() or not source_assets.is_dir():
        raise ValueError('旧数据库或原件目录无效')
    if destination.data.exists():
        raise ValueError('导入目标必须是尚不存在的新数据目录')
    if destination.data.is_relative_to(source_assets):
        raise ValueError('导入目标不能位于旧原件目录内')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
        if reader.execute('SELECT 1 FROM users WHERE id = ?', (owner_id,)).fetchone() is None:
            raise ValueError('必须显式选择旧库中的有效用户')
        if reader.execute("SELECT 1 FROM sqlite_master WHERE name = 'local_installations'").fetchone():
            if reader.execute('SELECT 1 FROM local_installations LIMIT 1').fetchone():
                raise ValueError('已有设备身份的数据库需使用恢复流程')
    destination.data.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.pka-import-', dir=destination.data.parent) as temporary:
        stage = Path(temporary) / 'data'
        stage.mkdir(mode=0o700)
        assets = stage / 'assets'
        assets.mkdir(mode=0o700)
        snapshot(source, stage / 'knowledge.db')
        for original in source_assets.rglob('*'):
            if original.is_symlink() or not (original.is_file() or original.is_dir()):
                raise ValueError('旧原件目录包含链接或特殊文件')
            copied = assets / original.relative_to(source_assets)
            if original.is_dir():
                copied.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(original, copied)
                get_platform().files.protect(copied)
                if file_digest(original) != file_digest(copied):
                    raise ValueError('原件复制校验失败')
        database_engine = make_engine(f'sqlite:///{stage / "knowledge.db"}')
        try:
            migrate(database_engine, stage / 'backups')
            with Session(database_engine) as db, db.begin():
                verify_assets(db, assets)
                now = datetime.now(timezone.utc)
                db.execute(update(UserSession).values(revoked_at=now))
                db.execute(update(AgentConnection).values(status='revoked', revoked_at=now))
                db.execute(update(Source).where(Source.owner_id == owner_id).values(upload_status='local_saved', updated_at=Source.updated_at))
            identity = initialize_identity(database_engine, credential, owner_id)
        finally:
            database_engine.dispose()
        # Publish only after all checks; never overwrite an existing data directory.
        if destination.data.exists():
            raise ValueError('导入目标在准备期间已被创建')
        get_platform().files.publish_directory(stage, destination.data)
    destination.prepare()
    return identity


def import_legacy(source: Path, source_assets: Path, destination: LocalPaths,
                  credential: str, owner_id: str, *, source_stopped: bool) -> dict:
    with data_lock(destination, exclusive=True, prepare=False):
        return _import_legacy(source, source_assets, destination, credential, owner_id,
                              source_stopped=source_stopped)
