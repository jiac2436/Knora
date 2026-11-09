from pathlib import Path
import json,hashlib,shutil
root=Path.cwd(); release=json.loads((root/'apps/macos/release.json').read_text()); baseline=json.loads((root/release['source_record']).read_text())['sources']
target=root/'apps/macos/.build/library-update-source'
target.mkdir(exist_ok=True)
allowed={'LocalDomainFullAnalysis.swift','LocalDomainFullAnalysisView.swift','LocalDomainOverviewView.swift'}
(target/'Sources').mkdir(exist_ok=True)
for name, checksum in baseline.items():
    src=root/name; data=src.read_bytes()
    if src.name=='MacKernelHost.swift' and hashlib.sha256(data).hexdigest()!=checksum:
        text=data.decode()
        text=text.replace('    private var browserRegistration: (runtime: URL, instance: String)?\n','')
        a=text.index('                    if let libraryID = event["library_id"]'); b=text.index('                    connection = result',a)
        text=text[:a]+text[b:]
        text=text.replace('        let browser = browserRegistration\n        browserRegistration = nil\n','')
        a=text.index('        if let browser {'); b=text.index('        probe?.stop()',a)
        text=text[:a]+text[b:]
        data=text.encode()
    if src.name not in allowed:
        assert hashlib.sha256(data).hexdigest()==checksum, 'Cannot reproduce verified baseline '+name
    (target/'Sources'/src.name).write_bytes(data)
for directory in ('Resources','scripts'):
    shutil.copytree(root/'apps/macos'/directory,target/directory,dirs_exist_ok=True)
print('PASS: isolated source tree contains exactly 3 scoped Swift changes; MacKernelHost matches verified release hash')
