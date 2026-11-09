"""Bounded local image downloads; failures are retried only by explicit request."""
import hashlib
from io import BytesIO
import time
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update

from app.config import settings
from app.images import IMAGE_FORMATS
from app.models import Source, SourceImage, WebReadMarkdown
from app.platform_support import get_platform
from app.services import current_web_base_revision
from app.web_images import BROWSER_IMAGE_METHOD, WEB_IMAGE_METHOD, image_references
from .web_reader import _require_public_url, WECHAT_USER_AGENT

MAX_DOCUMENT_BYTES = 200 * 1024 * 1024


def download_image(url: str, client_factory=httpx.Client, referer='https://mp.weixin.qq.com/'):
    headers = {'User-Agent': WECHAT_USER_AGENT, 'Referer': referer,
               'Accept': 'image/png,image/jpeg,image/webp,image/gif,*/*;q=0.5'}
    with client_factory(timeout=httpx.Timeout(15, connect=5), trust_env=False,
                        follow_redirects=False, headers=headers) as client:
        from urllib.parse import urljoin
        for _ in range(5):
            _require_public_url(url)
            with client.stream('GET', url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not response.headers.get('location'):
                        raise ValueError('图片跳转缺少地址')
                    url = urljoin(url, response.headers['location'])
                    continue
                if response.status_code != 200:
                    raise ValueError(f'图片下载返回 HTTP {response.status_code}')
                body = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > settings.max_image_bytes:
                        raise ValueError('单张图片超过 20 MB 限制')
                    if time.monotonic() - started > 30:
                        raise ValueError('图片下载超时')
                try:
                    with Image.open(BytesIO(body)) as image:
                        mime = IMAGE_FORMATS.get(image.format)
                        width, height = image.size
                        if mime is None or width * height > 30_000_000:
                            raise ValueError('仅支持不超过 3000 万像素的 PNG、JPEG、GIF、WebP 图片')
                        image.verify()
                except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
                    raise ValueError('图片文件无效或格式不支持') from None
                return bytes(body), mime, width, height
    raise ValueError('图片跳转次数过多')


def _active_image(db, image):
    source = db.get(Source, image.source_id)
    if source is None or source.deleted_at is not None:
        return None
    base = current_web_base_revision(db, source)
    return source if base and base.id == image.revision_id else None


def run_image_once(db, downloader=None) -> bool:
    image = db.scalar(select(SourceImage).where(SourceImage.retrieval_method.in_([WEB_IMAGE_METHOD, BROWSER_IMAGE_METHOD]),
        SourceImage.status == 'pending').order_by(SourceImage.revision_id, SourceImage.ordinal).limit(1))
    if image is None:
        return False
    identifier = image.id
    if _active_image(db, image) is None:
        image.status, image.failure_reason = 'unavailable', '原资料已删除或有新版本，请重新打开资料'
        db.commit()
        return True
    claimed = db.execute(update(SourceImage).where(SourceImage.id == identifier,
        SourceImage.status == 'pending').values(status='running')).rowcount
    db.commit()
    if not claimed:
        return True
    path = None
    try:
        db.refresh(image)
        artifact = db.get(WebReadMarkdown, image.revision_id)
        references = image_references(artifact.markdown) if artifact else []
        if image.ordinal >= len(references):
            raise ValueError('图片的原始地址不可用，请重新读取文章')
        # Do not hold a SQLite read transaction while waiting on the network.
        url = references[image.ordinal].url
        source = db.get(Source, image.source_id)
        referer = source.raw_url if image.retrieval_method == BROWSER_IMAGE_METHOD else 'https://mp.weixin.qq.com/'
        revision_id = image.revision_id
        db.commit()
        body, mime, width, height = downloader(url) if downloader else download_image(url, referer=referer or '')
        db.expire_all()
        image = db.get(SourceImage, identifier)
        if image is None or image.status != 'running':
            db.rollback()
            return True
        if _active_image(db, image) is None:
            image.status, image.failure_reason = 'unavailable', '原资料已删除或有新版本，图片下载已停止'
            db.commit()
            return True
        sizes = db.scalars(select(SourceImage.size_bytes).where(SourceImage.revision_id == revision_id,
            SourceImage.status == 'saved')).all()
        if sum(size or 0 for size in sizes) + len(body) > MAX_DOCUMENT_BYTES:
            raise ValueError('整篇图片超过 200 MB 限制')
        directory = settings.upload_dir / 'images'
        if directory.is_symlink() or settings.upload_dir.is_symlink():
            raise ValueError('图片原件目录不可用')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = str(uuid4())
        path = directory / key
        with path.open('xb') as output:
            get_platform().files.protect(path)
            output.write(body)
        image.status, image.failure_reason = 'saved', ''
        image.file_key, image.sha256 = key, hashlib.sha256(body).hexdigest()
        image.mime_type, image.width, image.height = mime, width, height
        image.size_bytes = len(body)
        db.commit()
        path = None
    except Exception as error:
        db.rollback()
        image = db.get(SourceImage, identifier)
        if image is not None and image.status == 'running':
            image.status = 'unavailable'
            image.failure_reason = str(error)[:500] if isinstance(error, ValueError) else '图片下载失败，请手动重试'
            db.commit()
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    return True
