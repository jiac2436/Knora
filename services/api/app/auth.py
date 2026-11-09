from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .errors import ApiError
from .models import AgentConnection, LocalInstallation, Session as UserSession


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "authentication_required", "需要有效的 Bearer 凭证")
    token = authorization[7:].strip()
    if not token:
        raise ApiError(401, "authentication_required", "需要有效的 Bearer 凭证")
    return token


@dataclass(frozen=True)
class AppPrincipal:
    owner_id: str


@dataclass(frozen=True)
class AgentPrincipal:
    owner_id: str
    connection_id: str


def require_app_principal(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> AppPrincipal:
    token = extract_bearer(authorization)
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
    now = datetime.now(timezone.utc)
    if session is None or session.revoked_at is not None or _as_utc(session.expires_at) <= now:
        raise ApiError(401, "invalid_session", "会话无效或已过期")
    if settings.environment == "local":
        installation = db.get(LocalInstallation, 1)
        if installation is None or installation.session_id != session.id or installation.owner_id != session.user_id:
            raise ApiError(401, "invalid_session", "会话不属于当前本地设备")
    return AppPrincipal(session.user_id)


def require_agent_principal(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> AgentPrincipal:
    token = extract_bearer(authorization)
    connection = db.scalar(
        select(AgentConnection).where(AgentConnection.token_hash == token_hash(token))
    )
    now = datetime.now(timezone.utc)
    if (
        connection is None
        or connection.status != "active"
        or connection.revoked_at is not None
        or _as_utc(connection.expires_at) <= now
    ):
        raise ApiError(401, "invalid_connection", "Agent 连接无效、已撤销或已过期")
    from .agent_connections import is_current
    if not is_current(db, connection):
        raise ApiError(401, "invalid_connection", "该客户端已有更新的连接，请使用当前连接")
    if settings.environment == "local":
        installation = db.get(LocalInstallation, 1)
        if installation is None or installation.owner_id != connection.owner_id:
            raise ApiError(401, "invalid_connection", "Agent 连接不属于当前本地用户")
    return AgentPrincipal(connection.owner_id, connection.id)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

