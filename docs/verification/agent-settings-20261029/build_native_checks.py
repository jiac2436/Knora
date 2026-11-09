from pathlib import Path
import json,plistlib,shutil,subprocess,tempfile,sys
root=Path.cwd();here=root/'docs/verification/agent-settings-20261029'
source=root/'apps/macos/.build/agent-settings-source/Sources'
build=root/'apps/macos/.build/agent-settings-checks';build.mkdir(exist_ok=True)
alternate=build/'AppForChecks.swift'
alternate.write_text((source/'App.swift').read_text().replace('@main struct KnowledgeInboxApp','struct KnowledgeInboxApp'))
common=['xcrun','swiftc','-parse-as-library','-target','arm64-apple-macosx14.0','-module-cache-path',str(build/'module-cache')]
common += [str(p) for p in source.glob('*.swift') if p.name!='App.swift'] + [str(alternate)]
if '--ui-only' not in sys.argv:
 with (here/'native-checks-build.log').open('w') as log:
  subprocess.run(common+[str(root/'apps/macos/scripts/LocalAgentConnectionsTest.swift'),'-o',str(build/'AgentChecks')],stdout=log,stderr=log,check=True)
 with (here/'native-checks.log').open('w') as log:
  subprocess.run([str(build/'AgentChecks')],stdout=log,stderr=log,check=True)
app=build/'AgentSettingsUITest.app'
(app/'Contents/MacOS').mkdir(parents=True,exist_ok=True)
resources=app/'Contents/Resources';resources.mkdir(exist_ok=True)
home=Path(tempfile.mkdtemp(prefix='agent-settings-ui-home-',dir=root/'apps/macos/.build'))
(resources/'state-path.txt').write_text(str(home))
(app/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleExecutable='AgentSettingsUITest',CFBundleIdentifier='local.pka.agent-settings-ui-test',CFBundleName='Agent接入验收',CFBundlePackageType='APPL',LSMinimumSystemVersion='14.0',NSHighResolutionCapable=True)))
with (here/'native-ui-build.log').open('w') as log:
 subprocess.run(common+[str(root/'apps/macos/scripts/AgentSettingsUITest.swift'),'-o',str(app/'Contents/MacOS/AgentSettingsUITest')],stdout=log,stderr=log,check=True)
subprocess.run(['ditto',str(root/'apps/macos/.build/agent-settings-runtime'),str(resources/'runtime')],check=True)
subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
print('PASS: native checks; isolated UI app ready',home)
