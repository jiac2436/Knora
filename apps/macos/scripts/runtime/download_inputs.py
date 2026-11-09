"""Download fixed build inputs and verify their exact lengths and SHA-256 hashes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import certifi

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / 'apps/macos/runtime/artifacts.json'


def download(item, directory):
    name = item['file']
    if Path(name).name != name:
        raise ValueError('Invalid input filename')
    destination = directory / name
    if destination.is_symlink():
        raise ValueError('Input cannot be a symbolic link')
    if destination.exists():
        if destination.stat().st_size == item['bytes'] and hashlib.sha256(destination.read_bytes()).hexdigest() == item['sha256']:
            return dict(file=name, sha256=item['sha256'], verified=True, cached=True)
        raise ValueError('Existing input checksum mismatch: ' + name)
    errors = []
    for url in item.get('urls', []) + [item['url']]:
        if urlsplit(url).scheme != 'https':
            raise ValueError('Build inputs require HTTPS')
        partial = destination.with_suffix('.partial')
        try:
            context = ssl.create_default_context(cafile=certifi.where())
            request = Request(url, headers={'User-Agent': 'Knora-build-inputs/1.0'})
            with urlopen(request, context=context, timeout=45) as response, partial.open('xb') as output:
                total, checksum = 0, hashlib.sha256()
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > item['bytes']:
                        raise ValueError('Input exceeds pinned length')
                    checksum.update(chunk)
                    output.write(chunk)
                if total != item['bytes'] or checksum.hexdigest() != item['sha256']:
                    raise ValueError('Input length or checksum mismatch')
            partial.rename(destination)
            return dict(file=name, url=url, sha256=item['sha256'], verified=True, cached=False)
        except (OSError, ValueError) as error:
            errors.append(str(error))
        finally:
            partial.unlink(missing_ok=True)
    raise ValueError(f'Unable to download {name}: {errors}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    items = json.loads(CONFIG.read_text())['artifacts']
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(lambda item: download(item, args.output), items))
    (args.output / 'download-receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    print(f'Verified {len(receipts)} pinned build inputs')


if __name__ == '__main__':
    main()
