"""Image references in generated webpage Markdown; originals stay version-bound."""
from dataclasses import dataclass
import re
from uuid import uuid4

from .models import SourceImage

MAX_WEB_IMAGES = 200
WEB_IMAGE_METHOD = 'wechat_ua'
BROWSER_IMAGE_METHOD = 'browser_public'
_IMAGE = re.compile(r'!\[((?:\\.|[^\\\]])*)\]\((?:<([^>\r\n]+)>|([^\s)]+))\)')


@dataclass(frozen=True)
class WebImageReference:
    ordinal: int
    url: str
    caption: str
    start: int
    end: int


def image_references(markdown: str) -> list[WebImageReference]:
    result = []
    for match in _IMAGE.finditer(markdown):
        url = match[2] or match[3]
        if not url.startswith(('https://', 'http://')):
            continue
        if len(result) >= MAX_WEB_IMAGES:
            raise ValueError('文章超过 200 张图片，当前无法完整归档')
        caption = re.sub(r'\\([\\\[\]])', r'\1', match[1])
        result.append(WebImageReference(len(result), url, caption[:500], match.start(), match.end()))
    return result


def enqueue_images(db, source, revision, markdown: str, method=WEB_IMAGE_METHOD):
    for reference in image_references(markdown):
        db.add(SourceImage(id=str(uuid4()), source_id=source.id, revision_id=revision.id,
            ordinal=reference.ordinal, block_id=f'{"网页" if method == BROWSER_IMAGE_METHOD else "微信"}图片 {reference.ordinal + 1}',
            caption=reference.caption, status='pending', retrieval_method=method))


def local_image_markdown(markdown: str, images) -> str:
    by_ordinal = {image.ordinal: image for image in images}
    text = markdown
    for reference in reversed(image_references(markdown)):
        image = by_ordinal.get(reference.ordinal)
        if image is None:
            replacement = '[图片未归档]'
        else:
            replacement = f'![图片 {reference.ordinal + 1}](know-image://{image.id})'
        text = text[:reference.start] + replacement + text[reference.end:]
    return text
