from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AppPrincipal, _as_utc, require_app_principal, token_hash
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import AccountToken, PasswordCredential, Session as UserSession, User, utcnow


router = APIRouter(prefix="/v1/auth", tags=["account"])
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GENERIC = "如果邮箱可用，我们会发送邮件说明下一步。"
_DUMMY_HASH = "scrypt$16384$8$5$AAAAAAAAAAAAAAAAAAAAAA==$MpskhvQ+u4s+/NkUkYXWnG5c+zrsMS4zFVAvjU1/ZZ8="


class EmailInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        result = value.strip().casefold()
        if not _EMAIL.fullmatch(result):
            raise ValueError("邮箱格式无效")
        return result


class RegisterInput(EmailInput):
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("称呼不能为空")
        return result


class LoginInput(EmailInput):
    password: str = Field(min_length=1, max_length=256)


class TokenInput(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class ResetConfirmInput(TokenInput):
    password: str = Field(min_length=12, max_length=256)


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=5,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt$16384$8$5${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (16384, 8, 5):
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=32,
                                maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def _mail_ready() -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise ApiError(503, "email_unavailable", "邮件服务未配置，暂不能注册或找回密码")


def send_account_mail(address: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = address
    message["Subject"] = subject
    message.set_content(body)
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ApiError(503, "email_unavailable", "邮件发送失败，请稍后重试", True) from exc


def _new_account_token(db: Session, user_id: str, purpose: str) -> str:
    value = secrets.token_urlsafe(32)
    db.add(AccountToken(user_id=user_id, purpose=purpose, token_hash=token_hash(value),
                        expires_at=utcnow() + timedelta(minutes=30)))
    db.flush()
    return value


def _session(db: Session, user: User) -> dict:
    token = f"pka_{secrets.token_urlsafe(32)}"
    db.add(UserSession(user_id=user.id, token_hash=token_hash(token),
                       expires_at=utcnow() + timedelta(days=30)))
    db.commit()
    return {"access_token": token, "token_type": "bearer",
            "user": {"id": user.id, "display_name": user.name}}


@router.post("/register", status_code=202)
def register(payload: RegisterInput, db: Session = Depends(get_db)) -> dict:
    _mail_ready()
    user = db.scalar(select(User).where(User.email == payload.email))
    if user and db.get(PasswordCredential, user.id):
        return {"message": _GENERIC}
    if user is None:
        user = User(email=payload.email, name=payload.name.strip())
        db.add(user)
        db.flush()
    db.add(PasswordCredential(user_id=user.id, password_hash=hash_password(payload.password)))
    code = _new_account_token(db, user.id, "verify")
    send_account_mail(payload.email, "验证知间账户邮箱",
                      f"请在知间 Mac 客户端输入以下一次性验证凭证：\n\n{code}\n\n30 分钟内有效。若非本人操作，请忽略。")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return {"message": _GENERIC}


@router.post("/verify")
def verify_email(payload: TokenInput, db: Session = Depends(get_db)) -> dict:
    item = db.scalar(select(AccountToken).where(
        AccountToken.token_hash == token_hash(payload.token), AccountToken.purpose == "verify"))
    if item is None or item.used_at is not None or _as_utc(item.expires_at) <= utcnow():
        raise ApiError(400, "invalid_token", "验证凭证无效或已过期")
    credential = db.get(PasswordCredential, item.user_id)
    user = db.get(User, item.user_id)
    if credential is None or user is None:
        raise ApiError(400, "invalid_token", "验证凭证无效或已过期")
    credential.verified_at = utcnow()
    item.used_at = utcnow()
    return _session(db, user)


@router.post("/sessions")
def password_login(payload: LoginInput, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.email == payload.email))
    credential = db.get(PasswordCredential, user.id) if user else None
    if credential is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise ApiError(401, "invalid_credentials", "邮箱或密码错误，或邮箱尚未验证")
    now = utcnow()
    if credential.locked_until and _as_utc(credential.locked_until) > now:
        raise ApiError(429, "login_locked", "登录尝试过多，请 15 分钟后重试")
    if not verify_password(payload.password, credential.password_hash) or credential.verified_at is None:
        credential.failed_attempts += 1
        if credential.failed_attempts >= 5:
            credential.locked_until = now + timedelta(minutes=15)
            credential.failed_attempts = 0
        db.commit()
        raise ApiError(401, "invalid_credentials", "邮箱或密码错误，或邮箱尚未验证")
    credential.failed_attempts = 0
    credential.locked_until = None
    return _session(db, user)


@router.delete("/sessions/current", status_code=204, response_class=Response)
def logout(authorization: str | None = Header(default=None),
           _: AppPrincipal = Depends(require_app_principal),
           db: Session = Depends(get_db)) -> Response:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    item = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
    if item:
        item.revoked_at = utcnow()
        db.commit()
    return Response(status_code=204)


@router.post("/password-reset", status_code=202)
def request_reset(payload: EmailInput, db: Session = Depends(get_db)) -> dict:
    _mail_ready()
    user = db.scalar(select(User).where(User.email == payload.email))
    credential = db.get(PasswordCredential, user.id) if user else None
    if credential:
        latest = db.scalar(select(AccountToken).where(
            AccountToken.user_id == user.id, AccountToken.purpose == "reset")
            .order_by(AccountToken.created_at.desc()).limit(1))
        if latest is None or _as_utc(latest.created_at) <= utcnow() - timedelta(minutes=1):
            code = _new_account_token(db, user.id, "reset")
            send_account_mail(payload.email, "重设知间账户密码",
                              f"请在知间 Mac 客户端输入以下一次性重设凭证：\n\n{code}\n\n30 分钟内有效。若非本人操作，请忽略。")
            db.commit()
    return {"message": _GENERIC}


@router.post("/password-reset/confirm")
def confirm_reset(payload: ResetConfirmInput, db: Session = Depends(get_db)) -> dict:
    item = db.scalar(select(AccountToken).where(
        AccountToken.token_hash == token_hash(payload.token), AccountToken.purpose == "reset"))
    if item is None or item.used_at is not None or _as_utc(item.expires_at) <= utcnow():
        raise ApiError(400, "invalid_token", "重设凭证无效或已过期")
    credential = db.get(PasswordCredential, item.user_id)
    if credential is None:
        raise ApiError(400, "invalid_token", "重设凭证无效或已过期")
    credential.password_hash = hash_password(payload.password)
    credential.verified_at = utcnow()
    credential.failed_attempts = 0
    credential.locked_until = None
    item.used_at = utcnow()
    db.execute(update(UserSession).where(UserSession.user_id == item.user_id,
                                         UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    db.commit()
    return {"message": "密码已更新，请重新登录"}
