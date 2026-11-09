"""Audit pinned macOS wheels against domestic mirrors; never install into user state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import ssl
import time
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

import certifi
from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[4]
MIRRORS = ['https://pypi.tuna.tsinghua.edu.cn/simple/', 'https://mirrors.aliyun.com/pypi/simple/']
CONTEXT = ssl.create_default_context(cafile=certifi.where())


def wheel_ranks(python_version):
    version = tuple(int(part) for part in python_version.split('.')[:2])
    platforms = list(mac_platforms((14, 0), 'arm64'))
    tags = list(cpython_tags(version, platforms=platforms)) + list(compatible_tags(
        version, 'cp' + ''.join(map(str, version)), platforms=platforms))
    return {tag: index for index, tag in reversed(list(enumerate(tags)))}


def fetch(url, maximum=20_000_000):
    with urlopen(Request(url, headers={'User-Agent': 'KnowledgeInbox-source-audit/1.0'}),
                 context=CONTEXT, timeout=30) as response:
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('Metadata exceeds limit')
        return data, response.url


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            value = dict(attrs).get('href')
            if value:
                self.links.append(value)


def audit(pair, cache, lock, rank):
    name, version = pair
    result = dict(name=name, version=version, mirrors=[])
    try:
        metadata_file = cache / f'{name}-{version}.json'
        if not metadata_file.exists():
            metadata_file.write_bytes(fetch(f'https://pypi.org/pypi/{name}/{version}/json')[0])
        metadata = json.loads(metadata_file.read_bytes())
        choices = []
        for item in metadata['urls']:
            if item['packagetype'] != 'bdist_wheel' or item.get('yanked'):
                continue
            _, _, _, wheel_tags = parse_wheel_filename(item['filename'])
            overlap = [rank[tag] for tag in wheel_tags if tag in rank]
            if overlap:
                choices.append((min(overlap), item))
        if not choices:
            raise ValueError('No compatible macOS arm64 wheel for the selected Python')
        item = min(choices, key=lambda pair: pair[0])[1]
        result.update(filename=item['filename'], bytes=item['size'], sha256=item['digests']['sha256'])
        section = re.search(r'^' + re.escape(name) + r'==' + re.escape(version) + r'\s.*?(?=^\S|\Z)', lock, re.M | re.S)
        if section is None or result['sha256'] not in section.group():
            raise ValueError('Wheel digest missing from current approved lock')
        for mirror in MIRRORS:
            probe = dict(index=mirror)
            try:
                data, final = fetch(mirror + name + '/')
                parser = Links()
                parser.feed(data.decode())
                matches = [urljoin(final, link) for link in parser.links
                           if unquote(urlsplit(link).path).rsplit('/', 1)[-1] == item['filename']]
                if not matches:
                    raise ValueError('Exact wheel not found')
                link = matches[0]
                if urlsplit(link).fragment != 'sha256=' + result['sha256']:
                    raise ValueError('Mirror metadata digest mismatch')
                probe.update(url=link.split('#')[0], listed=True)
            except Exception as error:
                probe['error'] = str(error)
            result['mirrors'].append(probe)
        destination = cache / item['filename']
        receipt = destination.with_suffix('.source.json')
        for probe in result['mirrors']:
            if not probe.get('listed'):
                continue
            try:
                start = time.monotonic()
                if destination.exists():
                    if destination.stat().st_size != result['bytes'] or hashlib.sha256(destination.read_bytes()).hexdigest() != result['sha256']:
                        raise ValueError('Cached wheel length/digest mismatch')
                    source = json.loads(receipt.read_text()) if receipt.exists() else {}
                    if source.get('url') == probe['url'] and source.get('sha256') == result['sha256']:
                        probe.update(download_verified=True, cached=True)
                        result['download_verified'] = True
                        break
                    # An unlabelled cached file does not prove this mirror was tested.
                if not probe.get('download_verified'):
                    partial = destination.with_suffix('.partial')
                    try:
                        with urlopen(Request(probe['url'], headers={'User-Agent': 'KnowledgeInbox-source-audit/1.0'}),
                                     context=CONTEXT, timeout=45) as response, partial.open('wb') as out:
                            probe['final_url'] = response.url
                            total = 0
                            while chunk := response.read(1024 * 1024):
                                total += len(chunk)
                                if total > result['bytes']:
                                    raise ValueError('Download exceeds expected length')
                                out.write(chunk)
                        if partial.stat().st_size != result['bytes'] or hashlib.sha256(partial.read_bytes()).hexdigest() != result['sha256']:
                            raise ValueError('Downloaded length/digest mismatch')
                        partial.replace(destination)
                        receipt.write_text(json.dumps(dict(url=probe['url'], sha256=result['sha256'])) + '\n')
                    finally:
                        partial.unlink(missing_ok=True)
                probe.update(download_verified=True, seconds=round(time.monotonic() - start, 2))
                result['download_verified'] = True
                break
            except Exception as error:
                probe['download_error'] = str(error)
    except Exception as error:
        result['error'] = str(error)
    print(name, version, 'PASS' if result.get('download_verified') else result.get('error', 'NO DOWNLOAD'), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=ROOT / 'apps/macos/runtime/requirements-macos-arm64.lock')
    parser.add_argument('--catalog', type=Path, help='Audit only the dependencies of an existing extension catalog')
    parser.add_argument('--python-version', default=json.loads((ROOT / 'apps/macos/runtime/artifacts.json').read_text())['python'])
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    lock = args.lock.read_text()
    dependencies = dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', lock, re.M))
    if args.catalog:
        dependencies = {}
        for item in json.loads(args.catalog.read_text())['packages'].values():
            dependencies.update(item['dependencies'])
    rank = wheel_ranks(args.python_version)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: audit(pair, args.cache, lock, rank), sorted(dependencies.items())))
    (args.output / 'wheel-audit.json').write_text(json.dumps(dict(target=f'CPython{args.python_version} macOS14 arm64',
        mirrors=MIRRORS, wheels=results), ensure_ascii=False, indent=2) + '\n')
    print('Verified', sum(bool(item.get('download_verified')) for item in results), '/', len(results), flush=True)
    if not results or not all(item.get('download_verified') for item in results):
        parser.exit(1, 'Some pinned wheels have no verified domestic download. See wheel-audit.json.\n')


if __name__ == '__main__':
    main()
