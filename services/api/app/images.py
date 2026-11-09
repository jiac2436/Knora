"""Owner-scoped image originals attached to a captured document."""
import hashlib
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from uuid import UUID
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import SourceImage, WebReadMarkdown, utcnow
from .platform_support import get_platform
from .services import require_source, current_web_base_revision
from .web_images import BROWSER_IMAGE_METHOD, WEB_IMAGE_METHOD, enqueue_images

router = APIRouter()
IMAGE_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


class ImageRetryRequest(BaseModel):
    image_id: UUID | None = None


@router.post('/v1/sources/{source_id}/images/retry', status_code=202)
def retry_web_images(source_id: str, payload: ImageRetryRequest,
                     principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    source = require_source(db, principal.owner_id, source_id)
    base = current_web_base_revision(db, source)
    if base is None or base.parser_version not in {'web-wechat-v1', 'browser-images-v1'}:
        raise ApiError(409, 'images_not_retryable', '此资料不支持网页图片重试')
    existing = db.scalar(select(SourceImage.id).where(SourceImage.revision_id == base.id).limit(1))
    added = 0
    if existing is None and payload.image_id is None:
        artifact = db.get(WebReadMarkdown, base.id)
        if artifact:
            try:
                enqueue_images(db, source, base, artifact.markdown,
                    BROWSER_IMAGE_METHOD if base.parser_version == 'browser-images-v1' else WEB_IMAGE_METHOD)
                db.flush()
            except ValueError as error:
                raise ApiError(422, 'image_limit_exceeded', str(error)) from error
            added = len(list(db.scalars(select(SourceImage.id).where(SourceImage.revision_id == base.id))))
    query = select(SourceImage).where(SourceImage.source_id == source.id,
        SourceImage.revision_id == base.id, SourceImage.retrieval_method.in_([WEB_IMAGE_METHOD, BROWSER_IMAGE_METHOD]))
    if payload.image_id is not None:
        query = query.where(SourceImage.id == str(payload.image_id))
    images = list(db.scalars(query))
    if payload.image_id is not None and not images:
        raise ApiError(404, 'not_found', '图片不存在或不属于当前文章版本')
    queued = 0
    for image in images:
        if image.status == 'unavailable':
            image.status, image.failure_reason = 'pending', ''
            queued += 1
    db.commit()
    return {'queued_count': queued + added}


def require_image(db, source_id, image_id):
    image = db.scalar(select(SourceImage).where(SourceImage.id == image_id, SourceImage.source_id == source_id))
    if image is None:
        raise ApiError(404, "not_found", "图片不存在")
    return image


def finalize_image(db, owner_id, source_id, image_id, path, file_key, size, digest):
    """Verify and attach one original, preserving HTTP/worker replay semantics."""
    source = require_source(db, owner_id, source_id)
    image = require_image(db, source_id, image_id)
    try:
        with Image.open(path) as original:
            mime = IMAGE_FORMATS.get(original.format)
            width, height = original.size
            if mime is None or width * height > 30_000_000:
                raise ApiError(422, "unsupported_image", "仅支持不超过 3000 万像素的 PNG、JPEG、GIF、WebP")
            original.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ApiError(422, "invalid_image", "图片文件无效或过大") from error

    def replay():
        db.refresh(image)
        if image.status != "saved":
            return None
        if image.sha256 != digest:
            raise ApiError(409, "image_content_conflict", "该图片编号已保存不同内容")
        return {"id": image.id, "status": "saved", "size_bytes": image.size_bytes}, True

    existing = replay()
    if existing:
        return existing
    result = db.execute(update(SourceImage).where(
        SourceImage.id == image.id, SourceImage.status.in_(["pending", "unavailable"])
    ).values(status="saved", file_key=file_key, sha256=digest, size_bytes=size,
             mime_type=mime, width=width, height=height, failure_reason=""))
    if result.rowcount != 1:
        existing = replay()
        if existing:
            return existing
        raise ApiError(409, "image_upload_conflict", "图片状态已改变，请重试")
    source.updated_at = utcnow()
    db.commit()
    return {"id": image.id, "status": "saved", "size_bytes": size}, False


@router.put("/v1/sources/{source_id}/images/{image_id}", status_code=201)
async def upload_image(source_id: str, image_id: str, request: Request, response: Response,
                       principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    source = require_source(db, principal.owner_id, source_id)
    image = require_image(db, source_id, image_id)
    directory = settings.upload_dir / "images"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_key = str(uuid.uuid4())
    path = directory / file_key
    committed = False
    try:
        checksum = hashlib.sha256()
        size = 0
        with path.open("xb") as output:
            get_platform().files.protect(path)
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_image_bytes:
                    raise ApiError(413, "image_too_large", "单张图片超过大小限制")
                checksum.update(chunk)
                output.write(chunk)
        result, replayed = finalize_image(db, principal.owner_id, source_id, image_id,
            path, file_key, size, checksum.hexdigest())
        committed = not replayed
        if replayed:
            response.status_code = 200
        return result
    finally:
        if not committed:
            path.unlink(missing_ok=True)


@router.get("/v1/sources/{source_id}/images/{image_id}")
def download_image(source_id: str, image_id: str, principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)):
    require_source(db, principal.owner_id, source_id)
    image = require_image(db, source_id, image_id)
    path = settings.upload_dir / "images" / (image.file_key or "missing")
    if image.status != "saved" or not path.is_file():
        raise ApiError(404, "image_not_saved", "图片原件尚未保存")
    return FileResponse(path, media_type=image.mime_type,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
