"""Platform-independent local layout with defaults supplied by the adapter."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from .platform_support import get_platform

BUNDLE_ID = "local.pka.knowledge-inbox"


def absolute_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("本地数据目录必须是绝对路径")
    path = path.resolve()
    if any(part.endswith(".app") for part in path.parts):
        raise ValueError("用户数据不能保存到应用安装包内")
    return path


@dataclass(frozen=True)
class LocalPaths:
    data: Path
    cache: Path
    logs: Path

    @property
    def database(self) -> Path:
        return self.data / "knowledge.db"

    @property
    def assets(self) -> Path:
        return self.data / "assets"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    def prepare(self) -> None:
        for path in (self.data, self.assets, self.backups, self.data / "staging",
                     self.data / "jobs", self.cache, self.logs):
            get_platform().files.directory(path)


def local_paths(data: str | Path | None = None, cache: str | Path | None = None,
                logs: str | Path | None = None) -> LocalPaths:
    defaults = get_platform().directories(Path.home(), BUNDLE_ID) if not all((data, cache, logs)) else None
    return LocalPaths(
        absolute_directory(data or defaults.data),
        absolute_directory(cache or defaults.cache),
        absolute_directory(logs or defaults.logs),
    )
