from pathlib import Path
import subprocess
root=Path(__file__).resolve().parents[3];folder=Path(__file__).parent;temporary=root/'apps/macos/.build/xhs-native-tests';temporary.mkdir(exist_ok=True)
text=(root/'apps/macos/Sources/TextReader.swift').read_text();request=text[text.index('struct TextReaderRequest'):text.index('/// Each cached')]
resource=text[text.index('struct ReaderImageResource:'):]
position=(root/'apps/macos/Sources/ReadingSession.swift').read_text().split('struct ReaderPDFPosition')[0]
(temporary/'Support.swift').write_text('import AppKit\nimport Foundation\n'+request+resource+'\n'+position+'''
struct API { let base=URL(string:"https://example.invalid")!;let token="synthetic";func downloadFile(_ path:String,into directory:URL,named name:String)async throws->URL{throw NSError(domain:"fixture",code:1)} }
struct DesktopIntegration { static let shared=DesktopIntegration();func open(_ url:URL){} }
func failure(_ message:String)->NSError{NSError(domain:"fixture",code:1,userInfo:[NSLocalizedDescriptionKey:message])}
''')
base=['xcrun','swiftc','-D','CAPTURE_QUEUE_TESTS','-parse-as-library','-module-cache-path',str(temporary/'module-cache'),str(temporary/'Support.swift'),*[str(root/'apps/macos/Sources'/n) for n in ('WebTextReader.swift','MarkedLib.swift','ReaderSourceMap.swift','ReadingPositionScript.swift','XiaohongshuReader.swift','SearchLocation.swift')]]
for name,test in [('Xiaohongshu',folder/'NativeTest.swift'),('SearchPassage',root/'apps/macos/scripts/SearchPassageReaderTest.swift')]:
 binary=temporary/name
 subprocess.run([*base,str(test),'-o',str(binary)],check=True)
 subprocess.run([str(binary)],check=True,timeout=40)
