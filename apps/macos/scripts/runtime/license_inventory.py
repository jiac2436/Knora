"""Keep an inspectable dependency/license inventory alongside each payload."""
import hashlib
import json
from pathlib import Path


def write_inventory(site, destination, installed, names):
    packages = []
    for name in sorted(names):
        distribution = installed[name]
        licenses = []
        for entry in distribution.files or []:
            path = Path(distribution.locate_file(entry))
            if not path.is_file() or not path.resolve().is_relative_to(site.resolve()):
                continue
            if any(word in path.relative_to(site).as_posix().lower() for word in ('license', 'licence', 'copying', 'notice', 'copyright')):
                licenses.append(dict(path=path.relative_to(site).as_posix(),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        metadata = distribution.metadata
        packages.append(dict(name=name, version=distribution.version,
            license_expression=metadata.get('License-Expression'),
            license=metadata.get('License'),
            license_classifiers=[value for value in metadata.get_all('Classifier', []) if value.startswith('License ::')],
            project_urls=metadata.get_all('Project-URL', []), license_files=licenses))
    destination.write_text(json.dumps(dict(schema_version=1, packages=packages,
        scope='Declared wheel metadata and retained license files; see THIRD_PARTY_NOTICES.md for native libraries and models'),
        ensure_ascii=False, indent=2) + '\n')
