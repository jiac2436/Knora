"""Isolated Python entrypoint for the bundled read-only local API bridge."""
from __future__ import annotations

from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mcp_bridge import bridge_configured
from app.mcp_server import main


if __name__ == "__main__":
    if not bridge_configured():
        raise ValueError("客户端 MCP 接入必须配置 PKA_API_URL 或 PKA_API_FILE")
    main()
