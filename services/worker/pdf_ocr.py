"""PDF text extraction with PaddleOCR fallback for image-only pages."""
from __future__ import annotations

import os
from pathlib import Path

MAX_PAGES = 100
MAX_PAGE_CHARS = 20_000


def _ocr_image(image, model: str) -> str:
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from app.runtime_extensions import require
    require("ocr")
    from paddleocr import PaddleOCR
    import numpy as np

    if model not in {"tiny", "small", "medium"}:
        raise ValueError("unsupported OCR model")
    local_models = os.getenv("PKA_OCR_MODELS_DIR", "")
    model_options = {}
    if local_models:
        for kind, option in (("det", "text_detection_model_dir"), ("rec", "text_recognition_model_dir")):
            directory = Path(local_models) / f"PP-OCRv6_{model}_{kind}_infer"
            if not directory.is_dir():
                raise ValueError("当前运行包未包含所选 OCR 模型，请选择 Small")
            model_options[option] = str(directory)
    if not hasattr(_ocr_image, "engines"):
        _ocr_image.engines = {}
    engines = _ocr_image.engines
    if model not in engines:
        engines[model] = PaddleOCR(
            text_detection_model_name=f"PP-OCRv6_{model}_det",
            text_recognition_model_name=f"PP-OCRv6_{model}_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
            **model_options,
        )
    results = engines[model].predict(np.asarray(image.convert("RGB")))
    lines = []
    for result in results:
        payload = getattr(result, "json", result)
        if callable(payload):
            payload = payload()
        payload = payload.get("res", payload)
        lines.extend(str(line).strip() for line in payload["rec_texts"] if str(line).strip())
    return "\n".join(lines)


def _render_ocr_page(page, number: int, model: str, ocr_image) -> str:
    width, height = page.get_size()
    if width * height * 4 > 16_000_000:
        raise ValueError(f"第 {number} 页尺寸过大，无法安全 OCR")
    bitmap = page.render(scale=2)
    try:
        return ocr_image(bitmap.to_pil(), model).strip()
    finally:
        bitmap.close()


def extract_pdf(path: Path, model: str, ocr_image=_ocr_image) -> list[tuple[int, str]]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        if len(pdf) > MAX_PAGES:
            raise ValueError(f"PDF 超过 {MAX_PAGES} 页上限")
        pages = []
        missing_ocr = False
        from app.runtime_extensions import available, ExtensionRequired
        for number in range(len(pdf)):
            page = pdf[number]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range().strip()
                finally:
                    textpage.close()
                if not text:
                    if ocr_image is _ocr_image and not available("ocr"):
                        missing_ocr = True
                    else:
                        text = _render_ocr_page(page, number + 1, model, ocr_image)
                if len(text) > MAX_PAGE_CHARS:
                    raise ValueError(f"第 {number + 1} 页文字超过上限")
                pages.append((number + 1, text))
            finally:
                page.close()
        if missing_ocr:
            blocked = ExtensionRequired("ocr")
            blocked.partial_pages = pages
            raise blocked
        return pages
    finally:
        pdf.close()


def extract_pdf_inspector(path: Path, model: str, ocr_image=_ocr_image) -> list[tuple[int, str]]:
    """Use pdf-inspector's layout-aware Markdown, with local PaddleOCR for routed pages."""
    from app.runtime_extensions import require, available, ExtensionRequired
    require("documents")
    import pdf_inspector
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        if len(pdf) > MAX_PAGES:
            raise ValueError(f"PDF 超过 {MAX_PAGES} 页上限")
        result = pdf_inspector.extract_pages_markdown(str(path))
        if len(result.pages) != len(pdf):
            raise ValueError("PDF 页面解析结果不完整")
        pages = []
        missing_ocr = False
        for index, extracted in enumerate(result.pages):
            if extracted.page != index:
                raise ValueError("PDF 页面顺序不正确")
            number = index + 1
            text = extracted.markdown.strip()
            if extracted.needs_ocr or not text:
                page = pdf[index]
                try:
                    if ocr_image is _ocr_image and not available("ocr"):
                        missing_ocr = True
                    else:
                        recognized = _render_ocr_page(page, number, model, ocr_image)
                        text = "\n\n".join(part for part in (text, recognized) if part)
                finally:
                    page.close()
            if len(text) > MAX_PAGE_CHARS:
                raise ValueError(f"第 {number} 页文字超过上限")
            pages.append((number, text))
        if missing_ocr:
            blocked = ExtensionRequired("ocr")
            blocked.partial_pages = pages
            raise blocked
        return pages
    finally:
        pdf.close()
