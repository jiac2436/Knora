"""Conservative, local keyword tags for saved content."""

from __future__ import annotations

import re

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .models import Source, SourceRevision, SourceTag, SourceTagState

RULE_VERSION = 1

# Rules deliberately prefer precision. Unknown topics stay untagged.
RULES: tuple[tuple[str, str], ...] = (
    ("编程", r"编程|程序员|写代码|代码|软件开发|python|javascript|typescript|swift|java\b|api\b"),
    ("人工智能", r"人工智能|机器学习|深度学习|大模型|神经网络|\bai\b|\bllm\b|rag\b"),
    ("产品", r"产品经理|产品设计|用户需求|用户体验|需求分析|产品规划"),
    ("设计", r"视觉设计|交互设计|界面设计|排版|设计系统|ui\b|ux\b"),
    ("阅读", r"读书|阅读|书评|读后感|书单"),
    ("工作", r"工作总结|职业规划|面试|职场|招聘|求职"),
    ("学习", r"学习计划|课程|考试|复习|教学|培训"),
    ("运动", r"跑步|健身|运动|游泳|骑行|训练计划"),
    ("健康", r"健康|医疗|疾病|营养|睡眠|饮食"),
    ("财务", r"投资|财务|理财|预算|股票|基金"),
    ("法律", r"法律|法规|合同|诉讼|合规"),
    ("旅行", r"旅行|旅游|行程|酒店|景点"),
)


def infer_tags(title: str, text: str) -> list[str]:
    content = f"{title}\n{text[:12000]}".casefold()
    return [name for name, pattern in RULES if re.search(pattern, content)][:4]


def replace_auto_tags(db: Session, source: Source) -> None:
    revision = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    names = infer_tags(source.title, revision.extracted_text if revision else "")
    db.execute(delete(SourceTag).where(SourceTag.source_id == source.id))
    db.add_all(SourceTag(source_id=source.id, name=name) for name in names)
    state = db.get(SourceTagState, source.id)
    if state is None:
        db.add(SourceTagState(source_id=source.id, revision_id=source.current_revision_id,
                              rule_version=RULE_VERSION))
    else:
        state.revision_id = source.current_revision_id
        state.rule_version = RULE_VERSION


def ensure_auto_tags(db: Session) -> None:
    """Backfill existing sources once per revision and rule version."""
    while True:
        page = db.scalars(
            select(Source).outerjoin(SourceTagState, SourceTagState.source_id == Source.id)
            .where(Source.deleted_at.is_(None), or_(
                SourceTagState.source_id.is_(None),
                SourceTagState.rule_version != RULE_VERSION,
                SourceTagState.revision_id.is_distinct_from(Source.current_revision_id),
            )).limit(100)
        ).all()
        if not page:
            return
        for source in page:
            replace_auto_tags(db, source)
        db.commit()
