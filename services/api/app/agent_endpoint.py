"""Private, credential-free endpoint discovery owned by the local host lifetime."""
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4
from .platform_support import get_platform


def _read(path: Path) -> dict | None:
    try:
        raw = get_platform().files.read(path, maximum=4096)
        if raw is None:
            return None
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Agent 端点文件格式无效")
        return result
    except (OSError, ValueError, UnicodeDecodeError):
        raise ValueError("Agent 端点文件不可用或格式无效") from None


@contextmanager
def published_endpoint(directory: Path, address: str, library_id: str):
    destination = directory / "agent-api.json"
    _read(destination)  # Refuse pre-existing links, foreign files and broad permissions.
    instance = uuid4().hex
    payload = {"api_url": address, "library_id": library_id, "instance_id": instance}
    get_platform().files.atomic_write(destination, json.dumps(payload).encode())
    try:
        yield destination
    finally:
        try:
            current = _read(destination)
            if current and current.get("instance_id") == instance:
                destination.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass  # Never erase another instance's replacement or follow a changed path.
