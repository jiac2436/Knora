"""Explicit local preparation and locks shared by API, worker and MCP."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import engine
from .local_paths import LocalPaths
from .platform_support import get_platform
from .migrations import CURRENT_VERSION, migrate
from .models import Library, LocalInstallation, Session as UserSession, User, new_id


@contextmanager
def data_lock(paths: LocalPaths, *, exclusive: bool, prepare: bool = True):
    path = paths.data.parent / f'.{paths.data.name}.service.lock'
    with ExitStack() as stack:
        try:
            stack.enter_context(get_platform().files.lock(path, exclusive=exclusive))
        except BlockingIOError:
            raise ValueError('本地数据正在使用或升级，请停止服务后重试') from None
        if prepare:
            paths.prepare()
        yield


@contextmanager
def service_guard():
    if settings.environment == 'local':
        with data_lock(settings.local_paths, exclusive=False):
            yield
    else:
        yield


def validate_credential(credential: str) -> None:
    if not 32 <= len(credential) <= 256 or not credential.isascii() or any(c.isspace() for c in credential):
        raise ValueError('本地凭证必须是 32 至 256 位无空白 ASCII 字符')


def initialize_identity(database_engine, credential: str, owner_id: str | None = None) -> dict:
    from .auth import token_hash
    validate_credential(credential)
    now = datetime.now(timezone.utc)
    with Session(database_engine) as db, db.begin():
        installation = db.get(LocalInstallation, 1)
        if installation is None:
            users = db.scalars(select(User)).all()
            if users:
                owner = db.get(User, owner_id) if owner_id else None
                if owner is None:
                    raise ValueError('现有数据库必须显式选择有效的 owner_id')
            else:
                if owner_id:
                    raise ValueError('指定的本地用户不存在')
                identifier = new_id()
                owner = User(id=identifier, email=f'local-{identifier}@device.invalid', name='本地用户')
                db.add(owner)
                db.flush()
            library = Library(owner_id=owner.id)
            db.add(library)
            db.flush()
            installation = LocalInstallation(id=1, device_id=new_id(), owner_id=owner.id, library_id=library.id)
            db.add(installation)
        else:
            if owner_id and owner_id != installation.owner_id:
                raise ValueError('不能切换已绑定的本地用户')
            library = db.get(Library, installation.library_id)
            if db.get(User, installation.owner_id) is None or library is None or library.owner_id != installation.owner_id:
                raise ValueError('本地身份关联不完整')
        digest = token_hash(credential)
        session = db.scalar(select(UserSession).where(UserSession.token_hash == digest))
        if session is not None and (session.id != installation.session_id or session.user_id != installation.owner_id):
            raise ValueError('凭证已被其他会话使用')
        if session is None:
            previous = db.get(UserSession, installation.session_id) if installation.session_id else None
            if previous is not None:
                previous.revoked_at = now
            session = UserSession(user_id=installation.owner_id, token_hash=digest, expires_at=now + timedelta(days=30))
            db.add(session)
            db.flush()
            installation.session_id = session.id
        else:
            session.expires_at = now + timedelta(days=30)
            session.revoked_at = None
        db.flush()
        connection = db.connection()
        if connection.dialect.name == 'sqlite':
            journal_table = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='replica_state'").first()
            journal_trigger = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name GLOB 'pka_change_*'").first()
            if journal_table is not None and journal_trigger is not None:
                from .local_changes import initialize_journal
                initialize_journal(connection, installation.library_id, installation.device_id)
        return {'owner_id': installation.owner_id, 'device_id': installation.device_id,
                'library_id': installation.library_id}


def main() -> None:
    parser = argparse.ArgumentParser(description='准备本地数据目录、数据库和设备身份')
    parser.add_argument('command', choices=['prepare', 'import-legacy'])
    parser.add_argument('--credential-stdin', action='store_true', required=True,
                        help='通过标准输入读取凭证，禁止在命令参数中传递凭证')
    parser.add_argument('--owner-id', help='已有数据库须显式指定用户 ID')
    parser.add_argument('--source-database', type=Path)
    parser.add_argument('--source-assets', type=Path)
    parser.add_argument('--source-stopped', action='store_true')
    args = parser.parse_args()
    if settings.environment != 'local':
        parser.error('初始化必须使用 PKA_ENVIRONMENT=local')
    try:
        raw = sys.stdin.buffer.read(258)
        if len(raw) > 257:
            raise ValueError('本地凭证过长')
        try:
            credential = raw.removesuffix(b'\n').decode('ascii')
        except UnicodeDecodeError:
            raise ValueError('本地凭证必须使用 ASCII 字符') from None
        validate_credential(credential)
        backup = None
        if args.command == 'import-legacy':
            if not args.source_database or not args.source_assets or not args.owner_id:
                raise ValueError('导入须指定源数据库、原件目录和 owner_id')
            from .local_import import import_legacy
            identity = import_legacy(args.source_database, args.source_assets, settings.local_paths,
                                     credential, args.owner_id, source_stopped=args.source_stopped)
        else:
            with data_lock(settings.local_paths, exclusive=True):
                backup = migrate(engine, settings.local_paths.backups)
                identity = initialize_identity(engine, credential, args.owner_id)
        print(json.dumps({**identity, 'schema_version': CURRENT_VERSION, 'backup': str(backup) if backup else None}))
    except ValueError as exc:
        parser.exit(1, f'{exc}\n')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
