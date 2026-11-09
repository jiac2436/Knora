import asyncio
import errno
import json
import sqlite3
from types import SimpleNamespace

from fastapi import Request
import pytest
from sqlalchemy.exc import OperationalError

from app import errors


@pytest.mark.parametrize('failure', [
    OSError(errno.ENOSPC, 'private asset path'),
    OperationalError('private SQL', {'secret': 'private parameter'}, sqlite3.OperationalError('database or disk is full')),
])
def test_local_capacity_errors_are_safe_and_retryable(monkeypatch, failure):
    monkeypatch.setattr(errors, 'settings', SimpleNamespace(environment='local'))
    response = asyncio.run(errors.storage_error_handler(Request({'type': 'http'}), failure))
    body = json.loads(response.body)
    assert response.status_code == 503 and body['code'] == 'local_storage_full'
    assert body['retryable'] is True and body['request_id']
    assert '释放空间' in body['message'] and 'private' not in response.body.decode()


@pytest.mark.parametrize('environment,failure', [
    ('development', OSError(errno.ENOSPC, 'disk full')),
    ('local', OSError(errno.EACCES, 'permission denied')),
    ('local', OperationalError(None, None, sqlite3.OperationalError('database is locked'))),
    ('local', OperationalError(None, None, RuntimeError('database or disk is full'))),
])
def test_other_errors_keep_original_failure(monkeypatch, environment, failure):
    monkeypatch.setattr(errors, 'settings', SimpleNamespace(environment=environment))
    with pytest.raises(type(failure)) as captured:
        asyncio.run(errors.storage_error_handler(Request({'type': 'http'}), failure))
    assert captured.value is failure
