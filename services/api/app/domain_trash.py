"""Transactional local domain deletion; original files remain in the trash."""
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .errors import ApiError
from .local_trash import require_local, restore_source_record
from .models import (Domain, DomainTrash, Source, DomainProcessingPreference,
                     DomainBuild, DomainFullAnalysisJob, DomainSummaryJob, utcnow)
from .services import require_domain, soft_delete_source

router = APIRouter(prefix="/v1/domains", dependencies=[Depends(require_local)])


def lock_domain(db, owner, identifier):
    changed = db.execute(update(Domain).where(Domain.id == identifier, Domain.owner_id == owner)
                         .values(name=Domain.name)).rowcount
    if not changed:
        raise ApiError(404, "not_found", "知识库不存在")


@router.get("/trash")
def list_deleted_domains(principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    rows = db.execute(select(Domain, DomainTrash).join(DomainTrash, Domain.id == DomainTrash.domain_id)
                      .where(Domain.owner_id == principal.owner_id).order_by(DomainTrash.deleted_at.desc())).all()
    return {"items": [{"id": d.id, "name": d.name, "deleted_at": t.deleted_at,
                        "source_count": len(json.loads(t.source_ids_json))} for d, t in rows]}


@router.delete("/{domain_id}")
def delete_domain(domain_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    lock_domain(db, principal.owner_id, domain_id)
    require_domain(db, principal.owner_id, domain_id)
    sources = list(db.scalars(select(Source.id).where(Source.domain_id == domain_id,
                   Source.owner_id == principal.owner_id, Source.deleted_at.is_(None))))
    for identifier in sources:
        soft_delete_source(db, principal.owner_id, identifier, commit=False)
    db.add(DomainTrash(domain_id=domain_id, owner_id=principal.owner_id, source_ids_json=json.dumps(sources)))
    # Remove automatic processing authorization. Restoration never resumes paid work.
    db.execute(delete(DomainProcessingPreference).where(DomainProcessingPreference.domain_id == domain_id))
    for model in (DomainBuild, DomainFullAnalysisJob, DomainSummaryJob):
        db.execute(update(model).where(model.domain_id == domain_id, model.status.in_(("pending", "running")))
                   .values(status="cancelled", completed_at=utcnow()))
    db.commit()
    return {"id": domain_id, "status": "trashed", "source_count": len(sources)}


@router.post("/{domain_id}/restore")
def restore_domain(domain_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    lock_domain(db, principal.owner_id, domain_id)
    record = db.get(DomainTrash, domain_id)
    if record is None or record.owner_id != principal.owner_id:
        raise ApiError(404, "not_found", "回收站知识库不存在")
    identifiers = json.loads(record.source_ids_json)
    db.delete(record)
    db.flush()
    restored = 0
    for identifier in identifiers:
        source = db.get(Source, identifier)
        # Explicitly purged content cannot be recovered; previously deleted content stays deleted.
        if source is None or source.processing_status == "deleted":
            continue
        if source.deleted_at is not None:
            restore_source_record(db, principal.owner_id, identifier, commit=False)
            restored += 1
    db.commit()
    return {"id": domain_id, "status": "restored", "source_count": restored}
