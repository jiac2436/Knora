"""Drain persistent model work before a device configuration is replaced.

The small gate is device state in cache, outside knowledge backups. Writers and
submission entry points serialize on the local installation's SQLite write lock.
Existing workflows may finish their checkpoints; new workflows cannot start.
"""
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import AIJob, RAGJob, DomainSummaryJob, DomainBuild, DomainFullAnalysisJob, LocalInstallation
from .platform_support import get_platform

router = APIRouter(prefix='/v1/ai/configuration')
TASKS = (AIJob, RAGJob, DomainSummaryJob, DomainBuild, DomainFullAnalysisJob)


def _path():
    return settings.local_paths.cache / 'model-switch-v1.json'


def _gate():
    try:
        raw = get_platform().files.read(_path(), maximum=4096)
        value = json.loads(raw) if raw is not None else None
    except (ValueError, OSError):
        raise ApiError(409, 'model_switch_invalid', '模型配置切换状态不可用，原文件已保留，请检查设备状态') from None
    if value is not None:
        try:
            if set(value) != {'version', 'switch_id'} or value['version'] != 1:
                raise ValueError()
            UUID(value['switch_id'])
        except (ValueError, TypeError, KeyError):
            raise ApiError(409, 'model_switch_invalid', '模型配置切换状态无效，请重新启动客户端') from None
    return value


def _lock(db):
    if settings.environment == 'local':
        db.execute(update(LocalInstallation).where(LocalInstallation.id == 1)
                   .values(device_id=LocalInstallation.device_id))


def require_accepting(db):
    if settings.environment != 'local':
        return
    _lock(db)
    if _gate() is not None:
        raise ApiError(409, 'model_configuration_pending', '模型配置正在等待后台任务完成，暂不接受新模型任务')


def clear_startup_gate():
    # Called only by the exclusively owned Host, before API/Worker accept work.
    if _gate() is not None:
        _path().unlink()


def _write_gate(value):
    get_platform().files.atomic_write(_path(), json.dumps(value).encode())


def switch_status(db, owner):
    counts = {'pending': 0, 'running': 0, 'unknown': 0}
    for model in TASKS:
        for status, total in db.execute(select(model.status, func.count()).where(
                model.owner_id == owner, model.status.in_(counts)).group_by(model.status)):
            counts[status] += total
    gate = _gate()
    return {'switch_id': gate['switch_id'] if gate else None,
            'can_apply': bool(gate and not counts['pending'] and not counts['running']),
            'tasks': counts, 'profile': settings.ai_profile,
            'state': 'waiting' if gate and (counts['pending'] or counts['running']) else 'ready' if gate else 'active'}


def _require_local():
    if settings.environment != 'local':
        raise ApiError(404, 'not_found', '接口不存在')


@router.get('/state')
def configuration_state(principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    _require_local()
    return switch_status(db, principal.owner_id)


@router.post('/prepare')
def prepare_switch(principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    _require_local()
    _lock(db)
    if _gate() is None:
        _write_gate({'version': 1, 'switch_id': str(uuid4())})
    result = switch_status(db, principal.owner_id)
    db.commit()
    return result


class SwitchSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    switch_id: UUID


@router.post('/cancel')
def cancel_switch(body: SwitchSelection, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    _require_local()
    _lock(db)
    gate = _gate()
    if gate and gate['switch_id'] != str(body.switch_id):
        raise ApiError(409, 'model_switch_conflict', '模型配置切换状态已变化，请刷新')
    if gate:
        _path().unlink()
    db.commit()
    return {'cancelled': True}
