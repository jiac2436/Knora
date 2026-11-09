"""Package only the verified lightweight App; never copy a user's extensions/state."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))
from verify_runtime import digest, verify


def run(*arguments):
    return subprocess.run(list(map(str, arguments)), check=True, capture_output=True)


def files(root):
    return {p.relative_to(root).as_posix(): {'link': os.readlink(p)} if p.is_symlink()
            else {'sha256': digest(p), 'mode': p.stat().st_mode & 0o777}
            for p in root.rglob('*') if p.is_file() or p.is_symlink()}


def check_app(app, release):
    if app.is_symlink() or not app.is_dir():
        raise ValueError('正式App必须是实际目录')
    if digest(app / 'Contents/MacOS/KnowledgeInbox') != release['native_binary_sha256']:
        raise ValueError('App与已验收发布记录不一致')
    runtime = app / 'Contents/Resources/runtime'
    verify(runtime, release['manifest_sha256'])
    manifest = json.loads((runtime / 'manifest.json').read_text())
    if manifest.get('profile') != 'base-with-cloud-ai':
        raise ValueError('仅允许打包已验收的轻量基础运行包')
    resources = {p.name for p in (app / 'Contents/Resources').iterdir()}
    expected = {'runtime', 'Capture', 'AppIcon.icns'}
    if resources not in (expected, expected | {'Legal'}):
        raise ValueError('App资源目录包含未预期内容，停止打包')
    if release.get('requires_legal') and 'Legal' not in resources:
        raise ValueError('此版本缺少开源许可材料，停止打包')
    if 'Legal' in resources:
        legal = app / 'Contents/Resources/Legal'
        if set(files(legal)) != {'LICENSE', 'THIRD_PARTY_NOTICES.md', 'marked/LICENSE'}:
            raise ValueError('开源许可目录内容不符合预期')
    if any((runtime / name).exists() for name in ['extensions', 'ocr-models', 'model']):
        raise ValueError('基础包不应携带扩展或模型目录')
    if any('site-packages/' + name + '/' in item['path']
           for item in manifest['files'] for name in ['paddle', 'paddleocr', 'cv2', 'onnxruntime']):
        raise ValueError('基础包包含可选推理依赖，停止打包')
    run('codesign', '--verify', '--deep', '--strict', app)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--application', type=Path, help='可显式使用已安装的同版App，仍须匹配release.json摘要')
    parser.add_argument('--release-record', type=Path, default=ROOT / 'apps/macos/release.json',
                        help='已通过验收的候选发行记录；不修改正式发布记录')
    args = parser.parse_args()
    release = json.loads(args.release_record.read_text())
    if not release['tests']['status'].startswith('passed'):
        raise ValueError('正式版本尚未通过验收')
    app = (args.application or ROOT / release['app']).absolute()
    check_app(app, release)
    tools = ROOT / 'apps/macos/.build/dmg-tools'
    if tools.is_dir():
        sys.path.insert(0, str(tools))
    try:
        import dmgbuild
        from ds_store import DSStore
    except ImportError as error:
        raise ValueError('请先按PACKAGING.md安装仅用于构建的dmg-requirements.txt依赖') from error
    if dmgbuild.__version__ != '1.6.7':
        raise ValueError('请使用锁定的dmgbuild 1.6.7')
    with (app / 'Contents/Info.plist').open('rb') as handle:
        info = plistlib.load(handle)
    version = info['CFBundleShortVersionString']
    if not re.fullmatch(r'\d+(?:\.\d+)*', version):
        raise ValueError('版本号不能用于安装包文件名')
    architecture = run('lipo', '-archs', app / 'Contents/MacOS/KnowledgeInbox').stdout.decode().strip()
    if architecture != 'arm64':
        raise ValueError('当前发行运行包仅验收Apple Silicon架构')
    signature = run('codesign', '-dv', app).stderr.decode()
    if 'Signature=adhoc' not in signature:
        raise ValueError('此脚本面向当前ad-hoc测试版本；正式发行签名须另行验收')
    output = (args.output or ROOT / f'apps/macos/dist/Knora-{version}-macOS-{architecture}.dmg').absolute()
    if output.suffix != '.dmg' or output.exists() or output.is_symlink():
        raise ValueError('请指定尚不存在的.dmg输出路径')
    if not output.parent.is_dir():
        raise ValueError('输出目录不存在')
    checksum_path = output.with_suffix('.dmg.sha256')
    if checksum_path.exists():
        raise ValueError('校验文件已存在，不能覆盖旧发行物')
    source_files = files(app)
    build = ROOT / 'apps/macos/.build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dmg-', dir=build) as temporary:
        work = Path(temporary)
        staging = work / 'staging'
        staging.mkdir()
        run('cp', '-cR', app, staging / 'Knora.app')
        if files(staging / 'Knora.app') != source_files:
            raise ValueError('复制后的App与正式包不同')
        generator = work / 'generate-background'
        run('xcrun', 'swiftc', '-module-cache-path', build / 'module-cache',
            ROOT / 'apps/macos/scripts/GenerateDMGBackground.swift', '-o', generator)
        background = work / 'background.png'
        run(generator, background, '1')
        run(generator, work / 'background@2x.png', '2')
        image = work / output.name
        dmgbuild.build_dmg(str(image), 'Knora', settings={
            'format': 'UDZO', 'filesystem': 'HFS+', 'compression_level': 9,
            'files': [str(staging / 'Knora.app')], 'symlinks': {'Applications': '/Applications'},
            'icon': str(app / 'Contents/Resources/AppIcon.icns'), 'background': str(background),
            'window_rect': ((160, 180), (680, 420)), 'default_view': 'icon-view',
            'show_toolbar': False, 'show_sidebar': False, 'show_status_bar': False,
            'show_pathbar': False, 'show_tab_view': False,
            'icon_size': 128, 'text_size': 14, 'label_pos': 'bottom',
            'arrange_by': None, 'scroll_position': (0, 0), 'show_icon_preview': False,
            'icon_locations': {'Knora.app': (176, 224), 'Applications': (504, 224)},
        })
        run('hdiutil', 'verify', image)
        mount = work / 'mounted'
        mount.mkdir()
        run('hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', mount, image)
        try:
            if {p.name for p in mount.iterdir() if not p.name.startswith('.')} != {'Knora.app', 'Applications'}:
                raise ValueError('DMG包含未预期条目')
            if (mount / '安装说明.txt').exists():
                raise ValueError('DMG不应包含安装说明文件')
            with DSStore.open(str(mount / '.DS_Store'), 'r') as store:
                window = store['.']['bwsp']
                icons = store['.']['icvp']
                if (window['WindowBounds'] != '{{160, 180}, {680, 420}}' or window['ShowToolbar']
                        or window['ShowSidebar'] or icons['backgroundType'] != 2
                        or icons['iconSize'] != 128 or store['Knora.app']['Iloc'] != (176, 224)
                        or store['Applications']['Iloc'] != (504, 224)):
                    raise ValueError('Finder窗口背景、尺寸或图标位置不符合设计')
            if not (mount / 'Applications').is_symlink() or os.readlink(mount / 'Applications') != '/Applications':
                raise ValueError('Applications快捷方式无效')
            check_app(mount / 'Knora.app', release)
            if files(mount / 'Knora.app') != source_files:
                raise ValueError('DMG中的App内容或权限发生变化')
            installed = work / 'copied-install'
            installed.mkdir()
            run('ditto', mount / 'Knora.app', installed / 'Knora.app')
            check_app(installed / 'Knora.app', release)
            if files(installed / 'Knora.app') != source_files:
                raise ValueError('模拟复制安装后App不同')
        finally:
            run('hdiutil', 'detach', mount)
        if files(app) != source_files:
            raise ValueError('打包期间正式App发生变化，停止输出')
        os.replace(image, output)
        checksum = digest(output)
        checksum_path.write_text(f'{checksum}  {output.name}\n')
    print(json.dumps({'dmg': str(output.relative_to(ROOT)) if output.is_relative_to(ROOT) else str(output),
        'bytes': output.stat().st_size, 'sha256': checksum, 'version': version, 'architecture': architecture,
        'minimum_macos': '14.5', 'signature': 'ad-hoc', 'notarized': False,
        'runtime_manifest_sha256': release['manifest_sha256'], 'native_binary_sha256': release['native_binary_sha256'],
        'checks': 'Finder layout/background, two visible items without README, image verification, read-only mount, signature, runtime, complete file/mode comparison, copy-install',
        'installer_layout': {'window': [680, 420], 'icon_size': 128, 'retina_background': True, 'readme_included': False},
        'source_application': str(app),
        'optional_extensions_included': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
