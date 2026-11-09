"""Device capture handoff v1; no provider credentials or arbitrary file paths."""
from typing import Literal
from uuid import UUID
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class ClientAsset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=512)
    sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @field_validator('key')
    @classmethod
    def relative_key(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts or '\\' in value or '\0' in value or str(path) != value:
            raise ValueError('暂存标识必须是安全相对路径')
        return value


class FeishuOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    executable: str = Field(default='', max_length=4096)
    profile: str = Field(default='', max_length=200)

    @field_validator('executable', 'profile')
    @classmethod
    def no_control(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError('CLI选项包含控制字符')
        return value


class CaptureSubmission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    id: str
    kind: Literal['file', 'link', 'note']
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(default='', max_length=102400)
    domain_id: str = Field(min_length=1, max_length=100)
    project_id: str | None = Field(default=None, max_length=100)
    file: ClientAsset | None = None
    feishu: FeishuOptions = Field(default_factory=FeishuOptions)
    # Only the legacy importer populates these internal fields.
    legacy_source_id: str | None = None
    legacy_document: dict | None = Field(default=None, exclude=True)
    legacy_sent: bool = False

    @field_validator('id')
    @classmethod
    def stable_identifier(cls, value):
        if str(UUID(value)).lower() != value.lower():
            raise ValueError('采集编号无效')
        return value  # Preserve the v1 Swift idempotency key's original case.

    @model_validator(mode='after')
    def content_shape(self):
        if self.kind == 'file' and self.file is None and not self.legacy_sent:
            raise ValueError('文件采集缺少原件标识')
        if self.kind != 'file' and self.file is not None:
            raise ValueError('非文件采集不能携带文件原件')
        if self.kind != 'file' and not self.text.strip() and not self.legacy_sent:
            raise ValueError('采集正文或链接不能为空')
        return self
