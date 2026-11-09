"""Real macOS URL delivery into production Workspace, using only a temporary library."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

from local_domain_runtime_test import Host, wait


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('binary', type=Path)
    args = parser.parse_args()
    runtime, binary = args.runtime.resolve(strict=True), args.binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='pka-ui-test.article-links.') as temporary:
        root = Path(temporary).resolve()
        home = root / 'home'
        data = home / 'Library/Application Support/local.pka.knowledge-inbox'
        host = Host(runtime, root, 'http://127.0.0.1:9')
        host.env.update(PKA_DATA_DIR=str(data), PKA_CACHE_DIR=str(home / 'Library/Caches/local.pka.knowledge-inbox'),
                        PKA_LOG_DIR=str(home / 'Library/Logs/local.pka.knowledge-inbox'))
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': '链接隔离验收'}, expected=201)['id']
            source = host.call('/v1/captures', dict(capture_type='text', title='链接合成文章', coverage='full_text',
                content={'text': '仅用于验证客户端链接跳转，不调用模型。'}, context={'domain_id': domain},
                client={'kind': 'desktop', 'version': 'article-link-test'}), expected=201)['id']
            library = host.call('/v1/me')['library_id']
        finally:
            host.stop()
        app = root / 'ArticleLinksTest.app'
        executable = app / 'Contents/MacOS/ArticleLinksTest'
        executable.parent.mkdir(parents=True)
        shutil.copy2(binary, executable)
        plist = dict(CFBundleIdentifier='local.pka.article-links-test.' + uuid4().hex,
            CFBundleExecutable='ArticleLinksTest', CFBundleName='文章链接隔离验收', CFBundlePackageType='APPL',
            LSMinimumSystemVersion='14.0', NSHighResolutionCapable=True,
            NSAppTransportSecurity={'NSAllowsLocalNetworking': True}, PKATestRoot=str(root), PKATestRuntime=str(runtime),
            CFBundleURLTypes=[dict(CFBundleURLName='pka-article-link-test', CFBundleTypeRole='Viewer',
                                  CFBundleURLSchemes=['knowledge-inbox'])])
        with (app / 'Contents/Info.plist').open('wb') as output:
            plistlib.dump(plist, output)
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True, capture_output=True)
        url = f'knowledge-inbox://library/{library}/source/{source}'
        pid = None
        def state(name):
            file = root / name
            return json.loads(file.read_text()) if file.exists() else None
        try:
            subprocess.run(['open', '-n', '-a', str(app), url], check=True)
            pid = wait(lambda: state('ready.json'), 'test app readiness', timeout=90)['pid']
            first = wait(lambda: state('opened.json'), 'cold URL delivery', timeout=90)
            assert first['source_id'] == source and first['visible'], first
            print('PASS: macOS冷启动URL→生产Workspace→正确知识库文章', flush=True)
            subprocess.run(['open', '-a', str(app), url], check=True)
            warm = wait(lambda: (value if (value := state('opened.json')) and value['event'] != first['event'] else None),
                        'warm URL delivery', timeout=30)
            assert warm['source_id'] == source and warm['visible'], warm
            print('PASS: 已运行客户端接收URL并显示文章', flush=True)
            (root / 'close').touch()
            wait(lambda: state('closed.json'), 'close test window', timeout=10)
            subprocess.run(['open', '-a', str(app), url], check=True)
            reopened = wait(lambda: (value if (value := state('opened.json')) and value['event'] != warm['event'] else None),
                            'closed window URL delivery', timeout=30)
            assert reopened['source_id'] == source and reopened['visible'], reopened
            print('PASS: 关闭主窗口后点击文章链接重新显示阅读窗口', flush=True)
        finally:
            (root / 'stop').touch()
            if pid:
                for _ in range(30):
                    try: os.kill(pid, 0)
                    except ProcessLookupError: break
                    time.sleep(.2)
                else:
                    os.kill(pid, signal.SIGTERM)
            registry = Path('/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister')
            if registry.exists():
                subprocess.run([str(registry), '-u', str(app)], capture_output=True, check=False)
    print('PASS: 原生链接验收完成，仅临时数据、随机测试钥匙串，无模型调用', flush=True)


if __name__ == '__main__':
    main()
