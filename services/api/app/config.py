from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .local_paths import LocalPaths, local_paths
from .runtime_extensions import activate

activate()


DEFAULT_DATABASE_URL = f"sqlite:///{(Path(__file__).resolve().parents[1] / 'pka-dev.db').as_posix()}"


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("PKA_DATABASE_URL", DEFAULT_DATABASE_URL)
    environment: str = os.getenv("PKA_ENVIRONMENT", "development")
    local_data_dir: str = os.getenv("PKA_DATA_DIR", "")
    local_cache_dir: str = os.getenv("PKA_CACHE_DIR", "")
    local_log_dir: str = os.getenv("PKA_LOG_DIR", "")
    max_capture_chars: int = int(os.getenv("PKA_MAX_CAPTURE_CHARS", "102400"))
    max_feishu_capture_chars: int = int(os.getenv("PKA_MAX_FEISHU_CAPTURE_CHARS", "1000000"))
    max_image_bytes: int = int(os.getenv("PKA_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
    evidence_snippet_chars: int = int(os.getenv("PKA_EVIDENCE_SNIPPET_CHARS", "1200"))
    upload_dir: Path = Path(os.getenv("PKA_UPLOAD_DIR", str(Path(__file__).resolve().parents[1] / "uploads")))
    max_file_bytes: int = int(os.getenv("PKA_MAX_FILE_BYTES", str(50 * 1024 * 1024)))
    smtp_host: str = os.getenv("PKA_SMTP_HOST", "")
    smtp_port: int = int(os.getenv("PKA_SMTP_PORT", "587"))
    smtp_user: str = os.getenv("PKA_SMTP_USER", "")
    smtp_password: str = os.getenv("PKA_SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("PKA_SMTP_FROM", "")
    semantic_enabled: bool = os.getenv("PKA_SEMANTIC_ENABLED", "1") == "1"
    semantic_min_similarity: float = float(os.getenv("PKA_SEMANTIC_MIN_SIMILARITY", "0.5"))
    semantic_cache_dir: Path = Path(os.getenv("PKA_SEMANTIC_CACHE_DIR", str(Path(__file__).resolve().parents[1] / "model-cache")))
    semantic_model_path: str = os.getenv("PKA_SEMANTIC_MODEL_PATH", "")
    web_allow_fake_ip: bool = os.getenv("PKA_WEB_ALLOW_FAKE_IP", "0") == "1"
    crawl4ai_url: str = os.getenv("PKA_CRAWL4AI_URL", "")
    crawl4ai_token: str = os.getenv("PKA_CRAWL4AI_TOKEN", "")
    ai_gateway_url: str = os.getenv("PKA_AI_GATEWAY_URL", "")
    ai_gateway_token: str = field(default=os.getenv("PKA_AI_GATEWAY_TOKEN", ""), repr=False)
    ai_profile: dict | None = None

    @property
    def local_paths(self) -> LocalPaths:
        return local_paths(self.local_data_dir or None, self.local_cache_dir or None,
                           self.local_log_dir or None)

    def __post_init__(self) -> None:
        if self.environment != "local":
            return
        paths = self.local_paths
        database_url = f"sqlite:///{paths.database.as_posix()}"
        if self.database_url not in {DEFAULT_DATABASE_URL, database_url}:
            raise ValueError("本地模式数据库必须位于指定用户数据目录")
        default_uploads = Path(__file__).resolve().parents[1] / "uploads"
        if self.upload_dir not in {default_uploads, paths.assets}:
            raise ValueError("本地模式原件必须位于指定用户数据目录")
        object.__setattr__(self, "database_url", database_url)
        object.__setattr__(self, "upload_dir", paths.assets)
        if self.semantic_cache_dir == Path(__file__).resolve().parents[1] / "model-cache":
            object.__setattr__(self, "semantic_cache_dir", paths.cache / "models")


settings = Settings()
