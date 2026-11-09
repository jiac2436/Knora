"""Isolated L0 smoke checks; no application imports, user data or model downloads."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

CHECKS = {
    "stdlib": """
import sqlite3, ssl, ctypes, zlib
with sqlite3.connect(':memory:') as db:
    assert db.execute('select 1').fetchone() == (1,)
assert ssl.OPENSSL_VERSION
""",
    "api": """
from fastapi import FastAPI
from fastapi.testclient import TestClient
import sqlalchemy, uvicorn, mcp
app = FastAPI()
@app.get('/probe')
def probe(): return {'ok': True}
with TestClient(app) as client:
    assert client.get('/probe').json() == {'ok': True}
""",
    "pdf": """
import pypdfium2 as pdfium
from PIL import Image
pdf = pdfium.PdfDocument.new()
page = pdf.new_page(32, 32)
bitmap = page.render()
assert bitmap.to_pil().size == (32, 32)
bitmap.close()
page.close()
pdf.close()
""",
    "ocr_runtime": """
import paddle
from paddleocr import PaddleOCR
assert float(paddle.to_tensor([2.0, 3.0]).sum()) == 5.0
""",
    "document_runtime": """
import pdf_inspector
from importlib.metadata import version
assert version('firecrawl-anydoc')
""",
    "embedding": """
import numpy as np
from fastembed import TextEmbedding
from pathlib import Path
import os
model_path = Path(os.environ['PKA_PROBE_MODEL'])
assert (model_path / 'model_optimized.onnx').is_file()
model = TextEmbedding(model_name='BAAI/bge-small-zh-v1.5',
    specific_model_path=str(model_path), cache_dir=os.environ['TMPDIR'],
    local_files_only=True)
values = list(model.embed(['本地知识库打包验证']))
assert len(values) == 1 and np.isfinite(values[0]).all()
assert np.linalg.norm(values[0]) > 0
""",
}


EXTRA_CHECKS = {
    "bundled_backend": """
import os, sys
from pathlib import Path
os.environ['PKA_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['PKA_UPLOAD_DIR'] = str(Path(os.environ['TMPDIR'])/'uploads')
os.environ['PKA_SEMANTIC_CACHE_DIR'] = str(Path(os.environ['TMPDIR'])/'models')
os.environ['PKA_SEMANTIC_ENABLED'] = '0'
os.environ['PKA_ENVIRONMENT'] = 'test'
sys.path.insert(0, os.environ['PKA_PROBE_BACKEND'])
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from services.worker.main import run_once
with TestClient(app) as client:
    assert client.get('/health').json()['status'] == 'ok'
    response = client.post('/v1/dev/sessions', json={'email': 'runtime@example.invalid', 'name': 'Runtime probe'})
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        assert run_once(db) is False
""",
    "native_math": """
import ctypes, sys
from pathlib import Path
import numpy as np
libs = next((Path(sys.prefix)/'lib').glob('python*/site-packages/paddle/libs'))
blas = ctypes.CDLL(str(libs/'libblas.dylib'))
lapack = ctypes.CDLL(str(libs/'liblapack.dylib'))
I = ctypes.c_int
D = ctypes.c_double
pdouble = ctypes.POINTER(D)
a = np.array([[1., 2.], [3., 4.]], order='F')
b = np.array([[2., 0.], [0., 2.]], order='F')
c = np.zeros((2, 2), order='F')
n = I(2); one = D(1); zero = D(0); trans = ctypes.c_char(b'N')
blas.dgemm_(ctypes.byref(trans), ctypes.byref(trans), ctypes.byref(n), ctypes.byref(n),
    ctypes.byref(n), ctypes.byref(one), a.ctypes.data_as(pdouble), ctypes.byref(n),
    b.ctypes.data_as(pdouble), ctypes.byref(n), ctypes.byref(zero),
    c.ctypes.data_as(pdouble), ctypes.byref(n))
assert np.allclose(c, a @ b)
a = np.array([[3., 1.], [1., 2.]], order='F')
b = np.array([9., 8.]); piv = np.zeros(2, dtype=np.int32); rhs = I(1); info = I()
lapack.dgesv_(ctypes.byref(n), ctypes.byref(rhs), a.ctypes.data_as(pdouble), ctypes.byref(n),
    piv.ctypes.data_as(ctypes.POINTER(I)), b.ctypes.data_as(pdouble), ctypes.byref(n), ctypes.byref(info))
assert info.value == 0 and np.allclose(b, [2., 3.])
dyld = ctypes.CDLL(None)
dyld._dyld_image_count.restype = ctypes.c_uint32
dyld._dyld_get_image_name.restype = ctypes.c_char_p
names = [dyld._dyld_get_image_name(i).decode() for i in range(dyld._dyld_image_count())]
assert not any('/opt/homebrew/' in name for name in names)
assert all(any(name.endswith(lib) for name in names) for lib in ('libgfortran.5.dylib','libgomp.1.dylib','libquadmath.0.dylib'))
""",
    "ocr_inference": """
from pathlib import Path
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from paddleocr import PaddleOCR
root = Path(os.environ['PKA_PROBE_OCR_MODELS'])
engine = PaddleOCR(text_detection_model_name='PP-OCRv6_small_det',
    text_detection_model_dir=str(root/'PP-OCRv6_small_det_infer'),
    text_recognition_model_name='PP-OCRv6_small_rec',
    text_recognition_model_dir=str(root/'PP-OCRv6_small_rec_infer'),
    use_doc_orientation_classify=False, use_doc_unwarping=False,
    use_textline_orientation=False, device='cpu')
image = Image.new('RGB', (900, 180), 'white')
ImageDraw.Draw(image).text((30, 50), 'LOCAL KNOWLEDGE 2026', font=ImageFont.load_default(size=48), fill='black')
results = list(engine.predict(np.asarray(image)))
lines = []
for result in results:
    payload = result.json
    if callable(payload): payload = payload()
    payload = payload.get('res', payload)
    lines.extend(payload['rec_texts'])
text = ''.join(lines).replace(' ', '').upper()
assert 'LOCALKNOWLEDGE2026' in text, text
""",
    "pdf_text_extraction": """
from pathlib import Path
import os
import pypdfium2 as pdfium
import pdf_inspector
text = 'Local knowledge archive sample. Evidence stays readable after relocation.'
stream = ('BT /F1 14 Tf 40 180 Td (' + text + ') Tj ET').encode()
objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
    b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 700 240] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    b'<< /Length ' + str(len(stream)).encode() + b' >>\\nstream\\n' + stream + b'\\nendstream']
buf = b'%PDF-1.4\\n'; offsets = [0]
for i, value in enumerate(objects, 1):
    offsets.append(len(buf)); buf += str(i).encode() + b' 0 obj\\n' + value + b'\\nendobj\\n'
xref = len(buf); buf += b'xref\\n0 6\\n0000000000 65535 f \\n'
for offset in offsets[1:]: buf += f'{offset:010} 00000 n '.encode() + b'\\n'
buf += b'trailer\\n<< /Size 6 /Root 1 0 R >>\\nstartxref\\n' + str(xref).encode() + b'\\n%%EOF\\n'
path = Path(os.environ['TMPDIR'])/'sample.pdf'; path.write_bytes(buf)
with pdfium.PdfDocument(str(path)) as pdf:
    page = pdf[0]; textpage = page.get_textpage()
    assert text in textpage.get_text_range()
    textpage.close(); page.close()
result = pdf_inspector.extract_pages_markdown(str(path))
assert len(result.pages) == 1 and result.pages[0].page == 0
assert 'Local knowledge archive sample' in result.pages[0].markdown
""",
    "document_conversion": """
import io, zipfile
import anydoc
csv = anydoc.to_markdown_bytes(b'Topic,Year\\nKnowledge,2026\\n', 'csv')
assert 'Knowledge' in csv and '2026' in csv and 'Topic' in csv
parts = {
    '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
    '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
    'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Local knowledge document 2026</w:t></w:r></w:p></w:body></w:document>',
}
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as archive:
    for name, content in parts.items(): archive.writestr(name, content)
assert 'Local knowledge document 2026' in anydoc.to_markdown_bytes(buf.getvalue())
""",
}


def run_checks(model: Path | None, timeout: int, ocr_models: Path | None = None,
               native_math: bool = False, backend: Path | None = None) -> dict:
    results = {}
    with tempfile.TemporaryDirectory(prefix="pka-runtime-probe-") as temp:
        checks = dict(CHECKS)
        checks['document_conversion'] = EXTRA_CHECKS['document_conversion']
        checks['pdf_text_extraction'] = EXTRA_CHECKS['pdf_text_extraction']
        if backend is not None:
            checks['bundled_backend'] = EXTRA_CHECKS['bundled_backend']
        if native_math:
            checks['native_math'] = EXTRA_CHECKS['native_math']
        if ocr_models is not None:
            checks['ocr_inference'] = EXTRA_CHECKS['ocr_inference']
        for name, code in checks.items():
            if name == "embedding" and model is None:
                results[name] = {"status": "not_tested", "reason": "model path required"}
                continue
            env = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": temp, "TMPDIR": temp,
                "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
                "PKA_PROBE_MODEL": str(model) if model else "",
                "OMP_NUM_THREADS": "1", "PADDLE_PDX_CACHE_HOME": str(Path(temp)/'paddlex'),
                "PKA_PROBE_OCR_MODELS": str(ocr_models) if ocr_models else "",
                "PKA_PROBE_BACKEND": str(backend) if backend else "",
            }
            # Reject Python network connections, including accidental model downloads.
            guard = """
import sys
def no_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('network disabled for runtime probe')
sys.addaudithook(no_network)
"""
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", guard + code],
                    cwd=temp, env=env, capture_output=True, text=True, timeout=timeout,
                )
                results[name] = {"status": "passed" if completed.returncode == 0 else "failed",
                                 "exit_code": completed.returncode}
                if completed.returncode:
                    results[name]["diagnostic"] = completed.stderr[-4000:]
            except subprocess.TimeoutExpired:
                results[name] = {"status": "failed", "reason": "timeout"}
    packages = sorted(
        ({"name": d.metadata["Name"], "version": d.version}
         for d in importlib.metadata.distributions()), key=lambda d: d["name"].lower()
    )
    return {
        "python": platform.python_version(), "architecture": platform.machine(),
        "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "checks": results, "packages": packages,
        "limits": ["OCR tested only when explicit local models are provided; synthetic sample is not a quality benchmark",
                   "document_conversion covers synthetic DOCX and CSV only",
                   "Python audit hook is not an OS network sandbox",
                   "does not prove clean-machine installation or signing"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--ocr-models', type=Path)
    parser.add_argument('--native-math', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.model is not None and not args.model.is_dir():
        parser.error('--model must be an existing directory')
    if args.ocr_models is not None:
        for name in ('PP-OCRv6_small_det_infer', 'PP-OCRv6_small_rec_infer'):
            if not (args.ocr_models / name / 'inference.json').is_file():
                parser.error('--ocr-models requires complete local small det/rec model directories')
    if args.backend is not None and not (args.backend / 'app/main.py').is_file():
        parser.error('--backend requires the bundled API source directory')
    result = run_checks(args.model.resolve() if args.model else None, args.timeout,
                        args.ocr_models.resolve() if args.ocr_models else None, args.native_math,
                        args.backend.resolve() if args.backend else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    for name, check in result['checks'].items():
        print(f"{name}: {check['status']}")
    return int(any(c['status'] == 'failed' for c in result['checks'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
