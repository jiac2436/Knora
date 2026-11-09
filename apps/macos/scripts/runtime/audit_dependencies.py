"""Query OSV using only public PyPI names/versions from the release lock."""
import argparse
import json
from pathlib import Path
import re
import ssl
from urllib.request import Request, urlopen

import certifi


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pairs = re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', args.lock.read_text(), re.M)
    if not pairs:
        parser.error('No pinned PyPI packages in lock')
    queries = [dict(package=dict(name=name, ecosystem='PyPI'), version=version) for name, version in pairs]
    request = Request('https://api.osv.dev/v1/querybatch', data=json.dumps(dict(queries=queries)).encode(),
                      headers={'Content-Type': 'application/json', 'User-Agent': 'Knora-dependency-audit/1.0'})
    with urlopen(request, context=ssl.create_default_context(cafile=certifi.where()), timeout=60) as response:
        results = json.load(response)['results']
    if len(results) != len(pairs):
        raise ValueError('Incomplete OSV response')
    findings = [dict(name=name, version=version, advisories=result['vulns'])
                for (name, version), result in zip(pairs, results) if result.get('vulns')]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dict(packages=len(pairs), findings=findings), indent=2) + '\n')
    print(f'OSV: {len(pairs)} pinned packages, {len(findings)} package versions with advisories')
    if findings:
        parser.exit(1, 'Review findings before release; a version match does not prove exploitability.\n')


if __name__ == '__main__':
    main()
