"""Run the native M4 capture contract against a real M5 packaged runtime."""
import base64
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image


def main(runtime, binary):
    with tempfile.TemporaryDirectory(prefix='pka-m5-native-') as temporary:
        root = Path(temporary).resolve()
        image = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, 'PNG')
        cli = root / 'lark-cli'
        cli.write_text('#!' + str(runtime / 'python/bin/python3') + ' -B\n' + '''
import base64,json,pathlib,sys
args=sys.argv[1:]
if '--version' in args: print('1.0.97'); raise SystemExit(0)
if '+media-download' in args:
 path=pathlib.Path('image.png'); path.write_bytes(base64.b64decode(PICTURE))
 value={'ok':True,'data':{'saved_path':str(path.resolve())}}
else:
 text='<img src="ImageToken" id="Block" alt="合成图"/>' if 'xml' in args else '# 合成文档\\n合成正文\\n![](ImageToken)'
 value={'ok':True,'identity':'user','data':{'document':{'document_id':'Document','revision_id':7,'content':text}}}
print(json.dumps(value))
'''.replace('PICTURE', repr(base64.b64encode(image.getvalue()).decode())))
        cli.chmod(0o700)
        environment = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL'}}
        subprocess.run([str(binary), str(runtime), str(root), str(cli)], env=environment, timeout=180, check=True)
        print('PASS: 原生采集使用候选自带Python、模型、CLI/OCR辅助程序；没有源码映射或仓库虚拟环境')


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True))
