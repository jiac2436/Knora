"""Run M4 from source with synthetic data and in-memory native credentials.

The temporary wrapper uses the repo venv; this is an engineering integration
test, not a standalone packaged runtime or M5 release acceptance.
"""
from pathlib import Path
import base64
from io import BytesIO
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image

REPO = Path(__file__).resolve().parents[3]


def main():
    binary, helper = map(Path, sys.argv[1:3])
    with tempfile.TemporaryDirectory(prefix='pka-m4-source-') as directory:
        root = Path(directory).resolve()
        runtime = root / 'runtime'
        python = runtime / 'python/bin/python3'
        python.parent.mkdir(parents=True)
        # Shell quoting is generated for fixed local paths; no user data or keys.
        import shlex
        python.write_text('#!/bin/sh\nexport PKA_SEMANTIC_ENABLED=0\nexport PKA_WORKER_ROOT='
                          + shlex.quote(str(runtime / 'backend')) + '\nexec '
                          + shlex.quote(str(REPO / '.venv/bin/python')) + ' "$@"\n')
        python.chmod(0o700)
        backend = runtime / 'backend'
        backend.mkdir()
        (backend / 'app').symlink_to(REPO / 'services/api/app', target_is_directory=True)
        (backend / 'services').mkdir()
        (backend / 'services/__init__.py').write_text('')
        for name in ('worker', 'ai_gateway'):
            (backend / 'services' / name).symlink_to(REPO / 'services' / name, target_is_directory=True)
        native = runtime / 'platform/macos/KnowledgePlatform'
        native.parent.mkdir(parents=True)
        shutil.copyfile(helper, native)
        native.chmod(0o700)
        image = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, 'PNG')
        picture = base64.b64encode(image.getvalue()).decode()
        cli = root / 'lark-cli'
        cli.write_text('#!' + str(REPO / '.venv/bin/python') + '\n' + '''
import base64,json,pathlib,sys,os
args=sys.argv[1:]
if 'orphan-test' in args:
 pathlib.Path(args[-1]).write_text(json.dumps({'cli':os.getpid(),'helper':os.getppid()}))
 os.execv('/bin/sleep',['sleep','30'])
if '--version' in args: print('1.0.97'); raise SystemExit(0)
if '+media-download' in args:
 file=pathlib.Path('image.png'); file.write_bytes(base64.b64decode(PICTURE))
 value={'ok':True,'data':{'saved_path':str(file.resolve())}}
else:
 content='<img src="ImageToken" id="Block" alt="合成图"/>' if 'xml' in args else '# 合成文档\\n合成正文\\n![](ImageToken)'
 value={'ok':True,'identity':'user','data':{'document':{'document_id':'Document','revision_id':7,'content':content}}}
print(json.dumps(value))
'''.replace('PICTURE', repr(picture)))
        cli.chmod(0o700)
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL'}}
        try:
            subprocess.run([str(binary), str(runtime), str(root), str(cli)], check=True, env=environment, timeout=120)
        except subprocess.CalledProcessError:
            for log in root.rglob('local-service.log'):
                print(log.read_text(errors='replace')[-6000:], file=sys.stderr)
            raise
        # Kill only our synthetic Python caller and verify the private pipe also
        # reclaims the helper and its CLI. No terminal or unrelated PID is touched.
        pid_file = root / 'orphan-pids.json'
        caller_code = "import sys;sys.path.insert(0,sys.argv[1]);from app.platform_support.external_macos import MacExternalCapabilities;MacExternalCapabilities(sys.argv[2]).run_cli(sys.argv[3],['orphan-test',sys.argv[4]],60,None,None)"
        temporary = root / 'helper-temporary'
        temporary.mkdir()
        caller = subprocess.Popen([str(REPO / '.venv/bin/python'), '-c', caller_code,
            str(REPO / 'services/api'), str(helper), str(cli), str(pid_file)],
            env={**environment, 'TMPDIR': str(temporary)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert pid_file.exists(), 'synthetic CLI did not start'
            identifiers = json.loads(pid_file.read_text())
            caller.kill()
            caller.wait(timeout=5)
            def alive(pid):
                try: os.kill(pid, 0); return True
                except ProcessLookupError: return False
            deadline = time.monotonic() + 6
            while any(alive(pid) for pid in identifiers.values()) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not any(alive(pid) for pid in identifiers.values()), 'orphaned helper/CLI'
            print('PASS: Python caller death reclaims native helper and CLI through the private pipe')
        finally:
            if caller.poll() is None:
                caller.kill(); caller.wait(timeout=5)


if __name__ == '__main__':
    main()
