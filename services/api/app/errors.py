from __future__ import annotations

import uuid
import errno
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse
from .config import settings


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, retryable: bool = False):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
            "retryable": exc.retryable,
        },
    )


async def storage_error_handler(request: Request, exc: Exception) -> JSONResponse:
    original = getattr(exc, 'orig', exc)
    full = (isinstance(original, OSError) and original.errno == errno.ENOSPC) or (
        isinstance(original, sqlite3.OperationalError) and
        (getattr(original, 'sqlite_errorcode', None) == 13 or str(original) == 'database or disk is full'))
    if settings.environment != 'local' or not full:
        raise exc
    return await api_error_handler(request, ApiError(
        503, 'local_storage_full', '本机存储空间不足，请释放空间后手动重试。', retryable=True))
