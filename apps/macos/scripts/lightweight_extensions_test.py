"""Real offline package install and local inference with temporary device state."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile


def probe(runtime, root):
    sys.path.insert(0, str(runtime / 'backend'))
    from app.runtime_extensions import activate, available, status
    activate()
    assert all(available(key) for key in ('semantic', 'ocr', 'documents')), status()
    def offline(event, arguments):
        if event == 'socket.connect':
            raise RuntimeError('Inference test forbids network connections')
    sys.addaudithook(offline)
    from app.semantic import embed_documents
    vectors = embed_documents(['这是轻量客户端的合成语义检索验收正文。'])
    assert len(vectors) == 1 and len(vectors[0]) > 0
    print('PASS: installed semantic package performs real offline embeddings', flush=True)
    from services.worker.anydoc_reader import extract_document
    document = root / 'test.csv'
    document.write_text('name,value\nlocal,42\n')
    assert '42' in extract_document(document, 'test.csv')
    print('PASS: installed document package converts real CSV locally', flush=True)
    from PIL import Image, ImageDraw, ImageFont
    from services.worker.pdf_ocr import _ocr_image
    image = Image.new('RGB', (800, 180), 'white')
    font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 48)
    ImageDraw.Draw(image).text((30, 50), 'LOCAL OCR 2026', fill='black', font=font)
    text = _ocr_image(image, 'small')
    assert 'LOCAL' in text.upper() and '2026' in text, repr(text)
    (root / 'saved-result.json').write_text(json.dumps({'text': text, 'vector_bytes': len(vectors[0])}))
    print('PASS: installed OCR package recognizes synthetic pixels with local models and no network', flush=True)


def exercise(runtime, artifacts):
    python = runtime / 'python/bin/python3'
    entry = runtime / 'backend/app/extension_install.py'
    catalog = json.loads((runtime / 'extension-catalog.json').read_text())
    with tempfile.TemporaryDirectory(prefix='pka-extension-runtime-') as temporary:
        root = Path(temporary).resolve()
        device = root / 'extensions'
        environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
        environment.update(HOME=str(root), TMPDIR=str(root), PKA_EXTENSIONS_ROOT=str(device),
            PKA_ENVIRONMENT='test', PKA_DATABASE_URL='sqlite:///:memory:',
            HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        def call(operation, value=None):
            arguments = [str(python), '-I', '-B', str(entry), '--root', str(device), operation]
            if value:
                arguments.append(str(value))
            output = subprocess.check_output(arguments, env=environment, text=True)
            return json.loads(output)
        for key in ('semantic', 'documents', 'ocr'):
            state = call('install', artifacts / catalog['packages'][key]['filename'])
            assert next(p for p in state['packages'] if p['id'] == key)['enabled']
            print(f'PASS: actual base interpreter installs and verifies {key} archive', flush=True)
        subprocess.run([str(python), '-I', '-B', str(Path(__file__).resolve()), str(runtime),
                        str(root), '--probe'], env=environment, check=True, timeout=180)
        saved = (root / 'saved-result.json').read_bytes()
        removed = call('remove', 'ocr')
        assert not next(p for p in removed['packages'] if p['id'] == 'ocr')['installed']
        assert (root / 'saved-result.json').read_bytes() == saved
        call('disable', 'semantic')
        command = ('import sys;sys.path.insert(0,sys.argv[1]);from app.config import settings;'
                   'from app.runtime_extensions import available;assert not available("semantic");'
                   'assert not settings.semantic_enabled;from app.main import app;'
                   'assert "numpy" not in sys.modules;print("PASS: disabling semantic preserves clean base startup")')
        subprocess.run([str(python), '-I', '-B', '-c', command, str(runtime / 'backend')],
                       env=environment, check=True, timeout=45)
        assert (root / 'saved-result.json').read_bytes() == saved
        print('PASS: uninstall removes only engine/models; generated data remains; enable state is restart-bound', flush=True)


if __name__ == '__main__':
    runtime = Path(sys.argv[1]).resolve(strict=True)
    if '--probe' in sys.argv:
        probe(runtime, Path(sys.argv[2]))
    else:
        exercise(runtime, Path(sys.argv[2]).resolve(strict=True))
