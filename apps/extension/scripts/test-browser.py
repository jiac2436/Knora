import http.server, threading, subprocess, tempfile, re, json, html
from functools import partial
from pathlib import Path
import os
root=str(Path(__file__).resolve().parents[1])
class Handler(http.server.SimpleHTTPRequestHandler):
 def log_message(self,*args): pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=root))
threading.Thread(target=server.serve_forever,daemon=True).start()
chrome=os.environ.get('CHROME_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
if not Path(chrome).is_file(): raise SystemExit('请通过 CHROME_BIN 指定本机 Chrome 可执行文件')
try:
 for name,outputid in [('xiaohongshu.html','results'),('xiaohongshu-popup.html','results'),('popup.html','browser-test-results')]:
  with tempfile.TemporaryDirectory(prefix='know-xhs-chrome-') as profile:
   host='127.0.0.1' if name=='popup.html' else 'www.xiaohongshu.com'
   args=[chrome,'--headless','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--no-proxy-server','--host-resolver-rules=MAP www.xiaohongshu.com 127.0.0.1','--user-data-dir='+profile,'--virtual-time-budget=4000',f'--unsafely-treat-insecure-origin-as-secure=http://www.xiaohongshu.com:{server.server_port}','--enable-logging=stderr','--dump-dom',f'http://{host}:{server.server_port}/tests/fixtures/{name}']
   r=subprocess.run(args,capture_output=True,text=True,timeout=30)
   match=re.search(r'<output\b[^>]*id="'+outputid+r'"[^>]*>(.*?)</output>',r.stdout,re.S)
   if not match:
    print(name,'FAIL fixture did not finish',r.returncode)
    print('\n'.join(line for line in r.stderr.splitlines() if 'CONSOLE' in line)[-1800:]); print(r.stderr[-300:]); raise SystemExit(1)
   results=json.loads(html.unescape(match.group(1)))
   print(name,json.dumps(results,ensure_ascii=False))
   assert all(x['passed'] for x in results)
finally:
 server.shutdown(); server.server_close()
