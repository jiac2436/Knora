"""Launch the managed MCP entry using the selected platform's private I/O."""
import json
import os
from pathlib import Path

from .platform_support import get_platform
from .platform_support.contracts import UnsupportedPlatform


def launch(runtime: Path, credential: Path):
    platform = get_platform()
    try:
        layout = platform.runtime_layout(runtime)
        raw = platform.files.read(credential, maximum=65536, parents=True)
        value = json.loads(raw) if raw is not None else None
        if (not isinstance(value, dict) or set(value) != {'command', 'args', 'env'}
                or value['command'] != str(layout.python)
                or value['args'] != ['-I', '-B', str(layout.backend / 'app/local_mcp.py')]
                or not isinstance(value['env'], dict)
                or set(value['env']) != {'PKA_API_FILE', 'PKA_AGENT_TOKEN'}
                or any(not isinstance(v, str) or not v or '\0' in v for v in value['env'].values())
                or not Path(value['env']['PKA_API_FILE']).is_absolute()):
            raise ValueError()
    except UnsupportedPlatform as error:
        raise SystemExit(str(error)) from None
    except (OSError, ValueError, TypeError):
        raise SystemExit('知识库连接凭证不可用或不安全，请在客户端检查连接') from None
    # The provider key and unrelated host credentials never reach the MCP process.
    environment = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL'}}
    environment.update(value['env'])
    platform.processes.exec(value['command'], value['args'], environment)
