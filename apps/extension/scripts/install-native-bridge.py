"""Install a user-scoped browser bridge against the existing signed desktop runtime.

No database writes or user account binding. The native helper reads the current
managed endpoint and device credential privately for each request.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--application', type=Path, default=ROOT / 'apps/macos/dist/Knora.app')
parser.add_argument('--home', type=Path, default=Path.home())
args = parser.parse_args()
application = args.application.resolve(strict=True)
runtime = application / 'Contents/Resources/runtime'
python = runtime / 'python/bin/python3'
if not python.is_file(): raise SystemExit('需要已安装完整运行环境的 Knora App')
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(application)], check=True)
bootstrap = 'import sys;sys.path.insert(0,sys.argv[1]);import runpy;script=sys.argv[2];sys.argv=[script,"--install"];runpy.run_path(script,run_name="__main__")'
request = {'home': str(args.home.resolve(strict=True)), 'runtime': str(runtime),
           'application': str(application), 'extension_path': str(ROOT / 'apps/extension/dist')}
result = subprocess.run([str(python), '-I', '-B', '-c', bootstrap,
                         str(runtime / 'backend'), str(ROOT / 'services/api/app/browser_native.py')],
                        input=json.dumps(request).encode(), capture_output=True, timeout=30)
if result.returncode:
    raise SystemExit('本地桥接注册失败，请检查安装路径或同名配置冲突。')
print('Chrome / Edge 本地桥接已安装。无需账户或 Token；重新加载扩展后自动发现 App。')
