"""Device model identity and bounded, private desktop startup messages.

Profiles contain no secrets. Credentials are only forwarded over inherited pipes;
the API installs public settings and the AI Worker installs the provider key.
"""
from __future__ import annotations

from typing import Literal
from types import SimpleNamespace
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import settings

BOOTSTRAP_VERSION = 1
MAX_BOOTSTRAP_BYTES = 32 * 1024


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    profile_id: UUID
    config_revision: int = Field(ge=1, le=2**31 - 1)
    mode: Literal['direct', 'external', 'disabled']
    status: Literal['ready', 'missing', 'denied', 'unavailable', 'invalid', 'disabled']
    provider: Literal['deepseek', 'openai-compatible'] = 'deepseek'
    base_url: str = Field(default='https://api.deepseek.com', max_length=2048)
    model: str = Field(default='', max_length=200)
    vision_model: str = Field(default='', max_length=200)
    credential_ref: str = Field(default='', max_length=512)
    credential_generation: UUID
    external_url: str = Field(default='', max_length=2048)

    @property
    def binding(self):
        if self.mode == 'direct':
            return f'http://127.0.0.1/embedded-ai/{self.profile_id}/{self.config_revision}'
        return self.external_url.rstrip('/')

    def public_binding(self):
        from services.ai_gateway.prompts import (PROMPT_VERSION, PROMPT_VERSION_V2, PROMPT_VERSION_V3,
                                                RAG_PROMPT_VERSION, DOMAIN_PROMPT_VERSION, SUMMARY_PROMPT_VERSION)
        return {**self.model_dump(mode='json'),
                'prompt_versions': {'analysis_v1': PROMPT_VERSION, 'analysis_v2': PROMPT_VERSION_V2,
                    'analysis_v3': PROMPT_VERSION_V3, 'rag': RAG_PROMPT_VERSION,
                    'node_v1': DOMAIN_PROMPT_VERSION, 'node_v2': SUMMARY_PROMPT_VERSION},
                'contract_versions': {'analysis': [1, 2, 3], 'rag': [1], 'node': [1, 2]},
                'capabilities': {'text': bool(self.model), 'images': bool(self.vision_model or self.model == 'deepseek-flash'),
                                 'structured_output': True}}


class ModelStartup(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    profile: ModelProfile
    provider_key: str = Field(default='', max_length=4096, repr=False)
    gateway_token: str = Field(default='', max_length=4096, repr=False)

    def validate_connection(self):
        if self.profile.status != 'ready' or self.profile.mode == 'disabled':
            if self.provider_key or self.gateway_token:
                raise ValueError('不可用模型配置不能携带凭证')
            return
        values = (self.provider_key, self.gateway_token)
        if any(not value.isascii() or any(c.isspace() or c == '\0' for c in value) for value in values if value):
            raise ValueError('模型凭证格式无效')
        if any(any(c in '\r\n\0' for c in value) for value in (self.profile.model, self.profile.vision_model)):
            raise ValueError('模型名称格式无效')
        from .ai import gateway_configured
        if not gateway_configured(SimpleNamespace(ai_gateway_url=self.profile.binding,
                                                  ai_gateway_token=self.gateway_token)):
            raise ValueError('模型连接配置无效')
        if self.profile.mode == 'external':
            if self.provider_key:
                raise ValueError('外置模式不能携带供应商凭证')
            return
        from services.ai_gateway.settings import GatewaySettings
        GatewaySettings(self.provider_key, self.profile.model, self.gateway_token,
            provider=self.profile.provider, base_url=self.profile.base_url,
            vision_model=self.profile.vision_model)


class HostStartup(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1]
    owner_credential: str = Field(min_length=32, max_length=256, repr=False)
    ai: ModelStartup | None = None
    client_contract_version: int = Field(default=1, ge=1)
    client_schema_version: int = Field(default=16, ge=1)


class WorkerStartup(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1]
    ai: ModelStartup | None = None


def read_message(stream, *, worker=False):
    raw = stream.readline(MAX_BOOTSTRAP_BYTES + 1)
    if len(raw) > MAX_BOOTSTRAP_BYTES or not raw.endswith(b'\n'):
        raise ValueError('私有启动消息缺失或超过大小限制')
    try:
        # Compatibility with an old client: only its local-owner token is read.
        if not worker and not raw.startswith(b'{'):
            credential = raw[:-1].decode('ascii')
            from .local_runtime import validate_credential
            validate_credential(credential)
            return HostStartup(version=1, owner_credential=credential)
        message = (WorkerStartup if worker else HostStartup).model_validate_json(raw)
        if message.ai:
            try:
                message.ai.validate_connection()
            except ValueError:
                # A well-formed but unusable AI configuration must not prevent
                # the authenticated local library from opening.
                message = message.model_copy(update={'ai': ModelStartup(
                    profile=message.ai.profile.model_copy(update={'status': 'invalid'}))})
        return message
    except (ValidationError, ValueError, TypeError):
        # Pydantic diagnostics may contain invalid input, including credentials.
        raise ValueError('私有启动消息格式或模型配置无效') from None


def encode_message(message):
    raw = message.model_dump_json().encode() + b'\n'
    if len(raw) > MAX_BOOTSTRAP_BYTES:
        raise ValueError('私有启动消息超过大小限制')
    return raw


def install_startup(ai, *, worker=False):
    """Called before the API/Worker starts serving; never write credentials."""
    profile = ai.profile if ai else None
    ready = bool(profile and profile.status == 'ready' and profile.mode != 'disabled')
    object.__setattr__(settings, 'ai_profile', profile.public_binding() if profile else None)
    object.__setattr__(settings, 'ai_gateway_url', profile.binding if ready else '')
    object.__setattr__(settings, 'ai_gateway_token', ai.gateway_token if ready else '')
    if worker and ready and profile.mode == 'direct':
        from services.ai_gateway.settings import GatewaySettings
        from .embedded_gateway import configure_embedded
        configure_embedded(GatewaySettings(ai.provider_key, profile.model, ai.gateway_token,
            provider=profile.provider, base_url=profile.base_url, vision_model=profile.vision_model), profile.binding)


def frozen_model_binding(configuration=None):
    profile = getattr(configuration or settings, 'ai_profile', None)
    return {'model_binding': profile} if profile else {}


def model_binding_matches(frozen, configuration=None):
    return frozen.get('model_binding') == getattr(configuration or settings, 'ai_profile', None)


def knowledge_snapshot(frozen):
    """Keep device connection metadata out of portable knowledge artifacts."""
    return {key: value for key, value in frozen.items() if key not in {'model_binding', 'gateway_url'}}


def worker_environment(environment):
    """Only the desktop runtime's explicit configuration reaches its Worker."""
    allowed = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'PKA_ENVIRONMENT',
        'PKA_DATA_DIR', 'PKA_CACHE_DIR', 'PKA_LOG_DIR', 'PKA_SEMANTIC_MODEL_PATH',
        'PKA_SEMANTIC_ENABLED', 'PKA_OCR_MODELS_DIR', 'PKA_WEB_ALLOW_FAKE_IP',
        'PKA_PLATFORM_HELPER', 'PKA_EXTENSIONS_ROOT', 'PKA_ACTIVE_EXTENSIONS',
        'PKA_CRAWL4AI_URL', 'PKA_CRAWL4AI_TOKEN'}
    return {key: value for key, value in environment.items() if key in allowed}
