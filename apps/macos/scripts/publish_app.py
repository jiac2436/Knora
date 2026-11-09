"""Promote one verified local application and archive previous deliveries."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
APP_FILENAME = 'Knora.app'
sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))
from verify_runtime import digest, verify


def write_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    source = args.source.absolute()
    record_path = args.record.resolve(strict=True)
    if source.is_symlink() or not source.is_dir() or source.suffix != '.app':
        raise ValueError('候选包必须是实际 .app 目录')
    source = source.resolve(strict=True)
    if not source.is_relative_to(ROOT) or not record_path.is_relative_to(ROOT):
        raise ValueError('候选包和验收记录必须位于本项目')
    record = json.loads(record_path.read_text())
    if not record.get('tests', {}).get('status', '').startswith('passed'):
        raise ValueError('验收记录尚未通过，不能发布')
    checksum = record['manifest_sha256']
    count = verify(source / 'Contents/Resources/runtime', checksum)
    binary = 'Contents/MacOS/KnowledgeInbox'
    binary_hash = digest(source / binary)
    expected_binary = record.get('unchanged_native_binary_sha256')
    if expected_binary and binary_hash != expected_binary:
        raise ValueError('可执行文件与验收记录不一致')
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(source)], check=True)
    running = subprocess.run(['lsof', '-a', '-n', '-c', 'Knowledge', '-d', 'txt', '-Fn'],
        capture_output=True, text=True)
    if running.returncode not in (0, 1):
        raise ValueError('无法确认客户端运行状态，停止发布')
    if any(line.startswith('n') and line.endswith('/Contents/MacOS/KnowledgeInbox')
            for line in running.stdout.splitlines()):
        raise ValueError('请先用 ⌘Q 退出客户端，发布不会强制结束进程')
    dist = ROOT / 'apps/macos/dist'
    previous = sorted(dist.glob('*.app'))
    if any(path.is_symlink() or not path.is_dir() for path in previous):
        raise ValueError('dist 中存在异常 App 路径，停止发布')
    if args.dry_run:
        print(f'PASS: 候选包校验通过；将归档 {len(previous)} 个旧包，正式入口为 dist/{APP_FILENAME}')
        return
    archive = ROOT / 'apps/macos/archive' / datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    archive.mkdir(parents=True, exist_ok=False)
    destination = dist / APP_FILENAME
    release_file = ROOT / 'apps/macos/release.json'
    if release_file.exists():
        shutil.copyfile(release_file, archive / 'previous-release.json')
    moved = []
    promoted = False
    staging_root = Path(tempfile.mkdtemp(prefix='release-', dir=ROOT / 'apps/macos/.build'))
    staged = staging_root / APP_FILENAME
    try:
        subprocess.run(['cp', '-cR', str(source), str(staged)], check=True)
        verify(staged / 'Contents/Resources/runtime', checksum)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(staged)], check=True)
        for path in previous:
            target = archive / path.name
            path.rename(target)
            moved.append((path, target))
        staged.rename(destination)
        promoted = True
        release = dict(app=str(destination.relative_to(ROOT)),
            source_record=str(record_path.relative_to(ROOT)), schema_version=record['schema_version'],
            plugin_version=record['plugin_version'], manifest_sha256=checksum,
            native_binary_sha256=binary_hash, runtime_files=count,
            publisher_sha256=digest(Path(__file__)),
            build_script_sha256=digest(ROOT / 'apps/macos/scripts/build.sh'),
            archive=str(archive.relative_to(ROOT)), archived_apps=[path.name for path, _ in moved],
            tests=record['tests'])
        write_json(archive / 'archive.json', release)
        write_json(release_file, release)
    except BaseException:
        if promoted:
            destination.rename(staged)
        for original, saved in reversed(moved):
            saved.rename(original)
        raise
    finally:
        shutil.rmtree(staging_root)
    print(f'PASS: 已发布 {destination}；归档 {len(moved)} 个旧包到 {archive}')


if __name__ == '__main__':
    main()
