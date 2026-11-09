import json
import os

import pytest

from app.agent_endpoint import published_endpoint


def test_private_endpoint_lifetime_and_restart(tmp_path):
    path = tmp_path / "agent-api.json"
    with published_endpoint(tmp_path, "http://127.0.0.1:8001", "library"):
        first = json.loads(path.read_text())
        assert first["api_url"] == "http://127.0.0.1:8001"
        assert set(first) == {"api_url", "library_id", "instance_id"}
        assert path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()
    with published_endpoint(tmp_path, "http://127.0.0.1:8002", "library"):
        assert json.loads(path.read_text())["instance_id"] != first["instance_id"]
    assert not path.exists()


def test_old_instance_does_not_erase_replacement(tmp_path):
    path = tmp_path / "agent-api.json"
    with published_endpoint(tmp_path, "http://127.0.0.1:8001", "library"):
        newer = {"api_url": "http://127.0.0.1:8002", "library_id": "library", "instance_id": "newer"}
        path.write_text(json.dumps(newer))
    assert json.loads(path.read_text()) == newer


@pytest.mark.parametrize("kind", ["link", "broad", "invalid", "oversized", "fifo"])
def test_publisher_refuses_unsafe_existing_file(tmp_path, kind):
    path = tmp_path / "agent-api.json"
    original = tmp_path / "original"
    original.write_text("keep")
    if kind == "link":
        path.symlink_to(original)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("x" * 4097 if kind == "oversized" else ("invalid" if kind == "invalid" else "{}"))
        os.chmod(path, 0o644 if kind == "broad" else 0o600)
    with pytest.raises(ValueError):
        with published_endpoint(tmp_path, "http://127.0.0.1:8001", "library"):
            pass
    assert original.read_text() == "keep"
    assert not list(tmp_path.glob(".agent-api-*.tmp"))
