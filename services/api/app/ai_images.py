"""Version-bound visual inputs; binary is loaded only for an explicit AI call."""
from __future__ import annotations

import base64
import hashlib
import re
from math import ceil
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .ai_contract import AnalysisRequest
from .errors import ApiError
from .models import SourceImage

MAX_VISUAL_WIRE_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 22 * 1024 * 1024
MAX_BATCH_IMAGES = 14  # Bound originals per call; large originals are tiled.
MAX_BATCH_TILES = 64
TILE_SIZE = 4096
TILE_STRIDE = 4000  # Overlap text and chart labels crossing a tile boundary.


def tile_count(width, height):
    return max(1, ceil(max(0, width - 96) / TILE_STRIDE)) * max(1, ceil(max(0, height - 96) / TILE_STRIDE))


def image_rows(db, source):
    from .services import current_web_base_revision
    base = current_web_base_revision(db, source)
    revision_id = base.id if base else source.current_revision_id
    return list(db.scalars(select(SourceImage).where(SourceImage.source_id == source.id,
        SourceImage.revision_id == revision_id).order_by(SourceImage.ordinal, SourceImage.id)))


def bind_images(db, source, snapshot, upload_dir: Path):
    rows = image_rows(db, source)
    if not rows:
        from .models import WebReadMarkdown
        from .services import current_web_base_revision
        from .web_images import image_references
        base = current_web_base_revision(db, source)
        markdown = db.get(WebReadMarkdown, base.id) if base and base.parser_version == 'web-wechat-v1' else None
        if markdown and image_references(markdown.markdown):
            raise ApiError(409, 'ai_images_not_archived', '文章包含图片，请先点击“补齐文章图片”，保存完成后再分析')
        return
    if len(rows) > 200:
        raise ApiError(409, 'ai_images_too_many', '图片超过 200 张，当前无法完整分析')
    if any(row.status != 'saved' for row in rows):
        raise ApiError(409, 'ai_images_not_ready', '请先等待文章图片保存完成；失败图片请重试后再进行 AI 分析')
    images, files, visual_evidence, anchors = [], {}, [], {}
    evidence = snapshot['evidence']
    placeholders = [(item, match.start()) for item in evidence
        for match in re.finditer(r'\[图片:[^\n]*?\]', item['text'])]
    for row in rows:
        if not row.sha256 or not row.file_key or not row.size_bytes or not row.width or not row.height:
            raise ApiError(409, 'ai_image_invalid', '图片信息不完整，请重新归档图片后分析')
        if row.width * row.height > 30_000_000 or tile_count(row.width, row.height) > MAX_BATCH_TILES:
            raise ApiError(409, 'ai_image_too_large', '图片超过 3000 万像素或需要超过 64 个图块，请拆分图片；本次不会缩小或忽略图片')
        path = upload_dir / 'images' / row.file_key
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row.size_bytes:
            raise ApiError(409, 'ai_image_missing', '图片原件缺失或已变化，请重新归档后分析')
        # Keep existing text evidence IDs. The adjacent image is an additional
        # original modality of that same source, never manufactured OCR text.
        direct = next((item for item in evidence if row.id in item['text']), None)
        if direct:
            anchor, position = direct, direct['text'].index(row.id)
        elif row.retrieval_method == 'wechat_ua':
            if row.ordinal >= len(placeholders):
                raise ApiError(409, 'ai_images_outside_coverage', '部分图片的原文位置不在本次文字分析范围，请拆分资料；本次未调用模型')
            anchor, position = placeholders[row.ordinal]
        else:
            anchor = next((item for item in evidence if row.caption and row.caption in item['text']), evidence[0])
            position = max(0, anchor['text'].find(row.caption))
        identifier = str(uuid5(NAMESPACE_URL, f'visual-evidence-v1:{row.id}:{anchor["id"]}'))
        start = max(0, position - 60)
        text = anchor['text'][start:start + 160]
        offset = anchor.get('text_start', 0) + start
        visual_evidence.append(dict(anchor, id=identifier, image_id=row.id, text=text,
            original_evidence_id=anchor.get('original_evidence_id') or anchor['id'],
            text_start=offset, text_end=offset + len(text)))
        anchors[row.id] = anchor['id']
        images.append(dict(id=row.id, evidence_id=identifier, ordinal=row.ordinal,
            sha256=row.sha256, mime_type=row.mime_type, size_bytes=row.size_bytes,
            width=row.width, height=row.height))
        files[row.id] = row.file_key
    snapshot['images'], snapshot['image_files'] = images, files
    snapshot['evidence'] = evidence + visual_evidence
    snapshot['analysis_plan']['image_count'] = len(images)
    snapshot['analysis_plan']['image_mode'] = 'joint'

    def batches(items):
        result, batch, size, tiles = [], [], 0, 0
        for image in items:
            count = tile_count(image['width'], image['height'])
            if batch and (len(batch) == MAX_BATCH_IMAGES or size + image['size_bytes'] > MAX_BATCH_BYTES or tiles + count > MAX_BATCH_TILES):
                result.append(batch); batch, size, tiles = [], 0, 0
            batch.append(image); size += image['size_bytes']; tiles += count
        if batch:
            result.append(batch)
        return result

    sections = snapshot.get('analysis_sections') or []
    if not sections and len(batches(images)) == 1 and len(snapshot['evidence']) <= 80 and sum(
            len(item['text']) for item in snapshot['evidence']) <= 48000:
        return
    if not sections:
        sections = [evidence]
    expanded, groups = [], []
    remaining = {image['id']: image for image in images}
    for section in sections:
        ids = {item['id'] for item in section}
        assigned = [image for image in images if anchors[image['id']] in ids and image['id'] in remaining]
        for image in assigned:
            remaining.pop(image['id'])
        for batch in batches(assigned) or [[]]:
            visual_ids = {image['evidence_id'] for image in batch}
            extra = [item for item in visual_evidence if item['id'] in visual_ids]
            combined = section + extra
            if len(combined) > 80 or sum(len(item['text']) for item in combined) > 48000:
                if not expanded or expanded[-1] != section:
                    expanded.append(section); groups.append([])
                combined = extra
            expanded.append(combined); groups.append([image['id'] for image in batch])
    if remaining:
        raise ApiError(409, 'ai_images_outside_coverage', '图片附近的正文超出本次分析范围，请缩小文章后再分析')
    if len(expanded) > 8:
        raise ApiError(409, 'ai_visual_plan_too_large', '图文分析超过 8 个分批的处理上限，请拆分文章；本次未调用模型')
    snapshot['analysis_sections'], snapshot['analysis_image_sections'] = expanded, groups
    plan = snapshot['analysis_plan']
    plan.update(sections=len(expanded), calls=len(expanded) + 1,
        section_details=[dict(index=i + 1, chars=sum(len(e['text']) for e in section),
            evidence_count=len(section), image_count=len(groups[i])) for i, section in enumerate(expanded)])


def images_current(db, source, snapshot):
    if not snapshot.get('images'):
        return True  # Historical text-only artifacts remain readable, never auto-paid.
    rows = image_rows(db, source)
    live = {row.id: (row.status, row.sha256, row.file_key, row.size_bytes) for row in rows}
    frozen = {image['id']: ('saved', image['sha256'], snapshot.get('image_files', {}).get(image['id']),
        image['size_bytes']) for image in snapshot['images']}
    if snapshot.get('phase') == 'segment':
        return all(live.get(identifier) == value for identifier, value in frozen.items())
    return live == frozen


def hydrate_images(request: AnalysisRequest, files: dict, upload_dir: Path) -> AnalysisRequest:
    if not request.images or request.phase == 'synthesis':
        return request
    images = []
    for image in request.images:
        key = files.get(image.id, '')
        if not key or Path(key).name != key:
            raise ValueError('Image file binding missing')
        path = upload_dir / 'images' / key
        if path.is_symlink() or path.parent.is_symlink() or upload_dir.is_symlink():
            raise ValueError('Image path unavailable')
        with path.open('rb') as original:
            body = original.read(20 * 1024 * 1024 + 1)
        if len(body) != image.size_bytes or hashlib.sha256(body).hexdigest() != image.sha256:
            raise ValueError('Image original changed')
        images.append(image.model_copy(update={'data_url': 'data:' + image.mime_type + ';base64,' +
            base64.b64encode(body).decode('ascii')}))
    hydrated = request.model_copy(update={'images': images})
    if len(hydrated.model_dump_json().encode()) > MAX_VISUAL_WIRE_BYTES:
        raise ValueError('Visual request exceeds wire budget')
    return hydrated
