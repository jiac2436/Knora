"""Immutable model configuration; secrets are excluded from representations."""
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GatewaySettings:
    provider_key: str = field(repr=False)
    model: str
    gateway_token: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 90.0
    vision_model: str = ""
    provider: str = "deepseek"

    @property
    def effective_vision_model(self):
        return self.vision_model.strip() or (self.model if self.model == 'deepseek-flash' else '')

    def __post_init__(self):
        if not self.provider_key.strip() or not self.model.strip() or not self.gateway_token.strip():
            raise ValueError("AI gateway requires provider key, model and gateway token")
        if self.provider not in {'deepseek', 'openai-compatible'}:
            raise ValueError('Unsupported provider interface')
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provider base URL must be HTTPS without credentials, query or fragment")
        if not 1 <= self.timeout_seconds <= 180:
            raise ValueError("Provider timeout must be between 1 and 180 seconds")
        if self.provider == 'deepseek' and self.vision_model and self.vision_model.strip() not in {'deepseek-flash', 'deepseek-v4-flash-vision-exp'}:
            raise ValueError('PKA_AI_VISION_MODEL must name a documented DeepSeek vision model: deepseek-flash')

    @classmethod
    def from_environment(cls):
        return cls(
            provider_key=os.environ.get("PKA_AI_PROVIDER_KEY", ""),
            model=os.environ.get("PKA_AI_MODEL", ""),
            gateway_token=os.environ.get("PKA_AI_GATEWAY_TOKEN", ""),
            base_url=os.environ.get("PKA_AI_PROVIDER_BASE_URL", "https://api.deepseek.com"),
            vision_model=os.environ.get("PKA_AI_VISION_MODEL", ""),
            provider=os.environ.get("PKA_AI_PROVIDER_KIND", "deepseek"),
        )
