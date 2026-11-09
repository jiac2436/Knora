from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class DevSessionCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=120)


class DomainCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    goal: str | None = None
    constraints: str | None = None
    background: str | None = None


class CaptureContent(BaseModel):
    text: str = ""
    image_urls: list[HttpUrl] = Field(default_factory=list, max_length=200)


class CaptureContext(BaseModel):
    domain_id: str
    project_id: str | None = None
    note: str | None = None
    save_reason: str | None = None


class CaptureClient(BaseModel):
    kind: Literal["mobile", "browser_extension", "desktop"]
    version: str = Field(min_length=1, max_length=80)


class FeishuDocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1, max_length=200)
    revision_id: int = Field(ge=0)
    cli_version: str = Field(min_length=1, max_length=80)


class FeishuImageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    ordinal: int = Field(ge=0, le=199)
    block_id: str = Field(default="", max_length=200)
    caption: str = Field(default="", max_length=500)
    status: Literal["pending", "unavailable"] = "pending"
    failure_reason: str = Field(default="", max_length=500)
    ocr_text: str = Field(default="", max_length=20000)
    retrieval_method: Literal["download", "preview"] = "download"


class CaptureCreate(BaseModel):
    capture_type: Literal["text", "url", "browser_page", "browser_selection", "feishu_document"]
    title: str | None = Field(default=None, max_length=500)
    source_url: HttpUrl | None = None
    captured_at: datetime | None = None
    content: CaptureContent
    coverage: Literal["full_text", "partial_text", "link_only"]
    context: CaptureContext
    client: CaptureClient
    feishu_document: FeishuDocumentMetadata | None = None
    feishu_images: list[FeishuImageMetadata] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_capture(self):
        if self.feishu_images:
            if self.capture_type != "feishu_document":
                raise ValueError("图片清单仅用于飞书文档")
            if len({image.id for image in self.feishu_images}) != len(self.feishu_images):
                raise ValueError("图片 ID 不得重复")
            if len({image.ordinal for image in self.feishu_images}) != len(self.feishu_images):
                raise ValueError("图片顺序不得重复")
        if self.capture_type == "feishu_document":
            host = (self.source_url.host or "").lower() if self.source_url else ""
            allowed = any(host == domain or host.endswith("." + domain)
                          for domain in ("feishu.cn", "larksuite.com", "larkoffice.com"))
            if not allowed or self.feishu_document is None or self.client.kind != "desktop":
                raise ValueError("飞书采集必须包含飞书链接、文档版本信息，并来自桌面客户端")
            if self.coverage == "link_only":
                raise ValueError("飞书采集必须包含已读取的正文")
        elif self.feishu_document is not None:
            raise ValueError("文档版本信息仅用于飞书采集")
        if self.capture_type == "text" and not self.content.text.strip():
            raise ValueError("text 类型必须包含 content.text")
        if self.capture_type == "url" and self.source_url is None:
            raise ValueError("url 类型必须包含 source_url")
        if self.coverage == "link_only" and self.source_url is None:
            raise ValueError("link_only 必须包含 source_url")
        if self.coverage != "link_only" and not self.content.text.strip():
            raise ValueError("full_text/partial_text 必须包含 content.text")
        return self


class ContextPatch(BaseModel):
    domain_id: str | None = None
    project_id: str | None = None
    project_ids: list[str] | None = None
    note: str | None = None
    save_reason: str | None = None
    applicability: str | None = None
    visibility: Literal["app_only", "app_and_agents"] | None = None
    version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_projects(self):
        if self.project_id is not None and self.project_ids is not None:
            raise ValueError("project_id 与 project_ids 不能同时提交")
        if self.project_ids is not None and len(self.project_ids) > 1:
            raise ValueError("M1 每条资料最多关联一个项目")
        return self


class ExperienceCreate(BaseModel):
    result: Literal["untried", "effective", "partially_effective", "ineffective"]
    notes: str | None = None
    project_id: str | None = None
    verified_at: datetime | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["exact", "semantic"] = "semantic"
    result_unit: Literal["source", "passage"] = "source"
    source_id: str | None = None
    task_context: str | None = Field(default=None, max_length=4000)
    project_id: str | None = None
    domain_ids: list[str] | None = None
    limit: int = Field(default=5, ge=1, le=20)
    tag_ids: list[str] | None = Field(default=None, max_length=50)
    tag_query: str | None = Field(default=None, max_length=120)


class SourceVersionsRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=50)


class ConnectionCreate(BaseModel):
    client_name: str = Field(min_length=1, max_length=160)
    domain_ids: list[str] = Field(min_length=1)
    expires_in_days: int = Field(default=30, ge=1, le=365)


class ConnectionUpdate(BaseModel):
    domain_ids: list[str] = Field(min_length=1)


class EvidenceRequest(BaseModel):
    evidence_id: str
    context_window: int = Field(default=0, ge=0, le=4000)


class SourceRequest(BaseModel):
    source_id: str


class SupplementCreate(BaseModel):
    text: str = Field(min_length=1, max_length=102400)


class RevisionCreate(BaseModel):
    text: str = Field(default="", max_length=1048576)
