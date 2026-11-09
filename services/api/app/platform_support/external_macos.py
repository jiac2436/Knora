"""Owned, bounded IPC to the bundled macOS CLI/Vision adapter."""
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from .contracts import UnsupportedPlatform
from .posix_files import PosixFiles


@dataclass(frozen=True)
class CommandResult:
    output: bytes
    errors: bytes
    exit_code: int


class MacExternalCapabilities:
    def __init__(self, helper=None):
        self.helper = Path(helper or os.getenv('PKA_PLATFORM_HELPER') or '/nonexistent-pka-platform-helper')

    def resolve_cli(self, path=''):
        home = Path.home()
        candidates = [Path(path).expanduser()] if path else [Path('/opt/homebrew/bin/lark-cli'),
            Path('/usr/local/bin/lark-cli'), home / '.local/bin/lark-cli']
        if not path:
            candidates += [Path(part) / 'lark-cli' for part in os.getenv('PATH', '').split(':') if part]
            versions = home / '.nvm/versions/node'
            if versions.is_dir():
                candidates += [item / 'lib/node_modules/@larksuite/cli/bin/lark-cli'
                               for item in sorted(versions.iterdir(), reverse=True)]
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.name == 'run.js' and candidate.parent.name == 'scripts':
                candidate = candidate.parent.parent / 'bin/lark-cli'
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        raise ValueError('未找到飞书CLI，请在飞书设置中选择可执行文件')

    def call(self, message, timeout):
        if not self.helper.is_absolute() or not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise UnsupportedPlatform('缺少同版系统辅助程序，请使用完整客户端运行包')
        raw = json.dumps({'version': 1, **message}, ensure_ascii=False).encode() + b'\n'
        if len(raw) > 65536:
            raise ValueError('系统能力请求超过大小限制')
        files = PosixFiles('darwin')
        with tempfile.TemporaryDirectory(prefix='pka-native-') as directory:
            output = Path(directory) / 'response.json'
            environment = {key: value for key, value in os.environ.items()
                           if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL'}}
            environment['PKA_PLATFORM_HELPER_OWNED'] = '1'
            process = None
            try:
                with output.open('xb') as stream:
                    files.protect(output)
                    process = subprocess.Popen([str(self.helper)], stdin=subprocess.PIPE, stdout=stream,
                        stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
                    process.stdin.write(raw)
                    process.stdin.flush()  # Keep stdin open: EOF cancels an orphaned helper.
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        if output.stat().st_size > 18 * 1024 * 1024 or time.monotonic() > deadline:
                            raise ValueError('系统辅助执行超时或返回内容过大')
                        time.sleep(0.05)
                response = files.read(output, maximum=18 * 1024 * 1024)
                try:
                    value = json.loads(response)
                    if not isinstance(value, dict) or value.get('version') != 1:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError('系统辅助程序返回格式无效') from None
                if process.returncode != 0 or value.get('ok') is not True:
                    raise ValueError({'start': '无法启动飞书CLI，请检查所选程序', 'timeout': '飞书CLI操作超时',
                        'outputLimit': '飞书CLI返回内容过大', 'cancelled': '系统辅助执行已取消',
                        'invalidImage': '图片格式无效或超过3000万像素'}.get(value.get('error'), '系统辅助执行失败'))
                return value
            finally:
                if process:
                    if process.stdin:
                        process.stdin.close()
                    if process.poll() is None:
                        # We created this session; never signal an unrelated process group.
                        try:
                            if os.getpgid(process.pid) == process.pid:
                                os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=5)

    def run_cli(self, executable, arguments, timeout, directory, maximum_directory_bytes):
        value = self.call({'action': 'cli', 'executable': str(executable), 'arguments': arguments,
            'timeout': timeout, 'directory': str(directory) if directory else None,
            'maximum_directory_bytes': maximum_directory_bytes}, timeout + 5)
        try:
            output, errors = (base64.b64decode(value[key], validate=True) for key in ('output', 'errors'))
            if len(output) + len(errors) > 12_000_000 or type(value['exit_code']) is not int:
                raise ValueError()
            return CommandResult(output, errors, value['exit_code'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('系统CLI响应无效') from None

    def recognize_image(self, path):
        value = self.call({'action': 'ocr', 'file': str(path)}, 120)
        text = value.get('text')
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError('系统OCR响应无效')
        return text
