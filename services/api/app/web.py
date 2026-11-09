"""Account web reader preference and webpage Markdown preview."""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .errors import ApiError
from .models import WebReadPreference
from .services import current_web_base_revision, current_web_markdown, require_source

router = APIRouter()


class WebReadPreferenceUpdate(BaseModel):
    engine: Literal["legacy", "crawl4ai"]
    cleaning_enabled: bool | None = None


@router.get("/v1/web/preferences")
def get_preference(principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    preference = db.get(WebReadPreference, principal.owner_id)
    return {"engine": preference.engine if preference else "legacy",
            "cleaning_enabled": preference.cleaning_enabled if preference else False}


@router.put("/v1/web/preferences")
def set_preference(payload: WebReadPreferenceUpdate,
                   principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    preference = db.get(WebReadPreference, principal.owner_id)
    if preference is None:
        preference = WebReadPreference(owner_id=principal.owner_id, engine=payload.engine,
                                       cleaning_enabled=payload.cleaning_enabled or False)
        db.add(preference)
    else:
        preference.engine = payload.engine
        if payload.cleaning_enabled is not None:
            preference.cleaning_enabled = payload.cleaning_enabled
    db.commit()
    return {"engine": preference.engine, "cleaning_enabled": preference.cleaning_enabled}


@router.get("/v1/sources/{source_id}/crawl4ai-markdown")
def get_markdown(source_id: str, principal: AppPrincipal = Depends(require_app_principal),
                 db: Session = Depends(get_db)) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    revision = current_web_base_revision(db, source)
    markdown = current_web_markdown(db, source, revision)
    if revision is None or markdown is None:
        raise ApiError(404, "crawl4ai_markdown_not_found", "当前网页没有可预览的 Markdown")
    return {"revision": revision.version, "markdown": markdown[0], "origin": markdown[1]}
