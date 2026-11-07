"""Document storage. Every uploaded file is normalised to PDF so it can be
previewed, annotated and printed the same way.

Layout on disk (one directory per document):
    documents/<id>/meta.json
    documents/<id>/document.pdf
    documents/<id>/annotations.json   (optional)
"""
from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError

ID_RE = re.compile(r"^[0-9a-f]{32}$")
IMAGE_TYPES = {"PNG", "JPEG", "TIFF", "GIF", "BMP", "WEBP"}


class DocumentError(ValueError):
    pass


class NotFound(LookupError):
    pass


@dataclass
class DocumentMeta:
    id: str
    name: str
    pages: int
    size: int
    created: float
    source: str = "upload"  # "upload" | "scan" | "annotated"
    has_annotations: bool = False


def safe_name(name: str, default: str = "document") -> str:
    name = Path(name or "").name  # drop any path components
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    return name[:120] or default


def to_pdf(data: bytes) -> bytes:
    """Return PDF bytes for an uploaded PDF or image. Raises DocumentError otherwise."""
    if data[:5] == b"%PDF-":
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise DocumentError("Encrypted PDFs are not supported")
            if len(reader.pages) == 0:
                raise DocumentError("PDF has no pages")
        except (PdfReadError, OSError) as exc:
            raise DocumentError(f"Invalid PDF: {exc}") from exc
        return data
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in IMAGE_TYPES:
            raise DocumentError(f"Unsupported image type {img.format}")
        frames = []
        for i in range(getattr(img, "n_frames", 1)):  # multi-page TIFF
            img.seek(i)
            frames.append(img.convert("RGB"))
        dpi = img.info.get("dpi", (150, 150))[0] or 150
        out = io.BytesIO()
        frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=float(dpi))
        return out.getvalue()
    except DocumentError:
        raise
    except Exception as exc:  # PIL raises a variety of exceptions for garbage input
        raise DocumentError("Unsupported file type: upload a PDF or an image (PNG, JPEG, TIFF)") from exc


class DocumentStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------
    def _dir(self, doc_id: str) -> Path:
        if not ID_RE.match(doc_id or ""):
            raise NotFound(doc_id)
        d = self.root / doc_id
        if not (d / "meta.json").is_file():
            raise NotFound(doc_id)
        return d

    def _write_meta(self, meta: DocumentMeta) -> None:
        path = self.root / meta.id / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(meta)))
        tmp.replace(path)

    # -- public API ----------------------------------------------------------
    def add(self, name: str, data: bytes, source: str = "upload") -> DocumentMeta:
        pdf = to_pdf(data)
        pages = len(PdfReader(io.BytesIO(pdf)).pages)
        doc_id = uuid.uuid4().hex
        d = self.root / doc_id
        d.mkdir()
        (d / "document.pdf").write_bytes(pdf)
        stem = Path(safe_name(name)).stem
        meta = DocumentMeta(
            id=doc_id, name=f"{stem}.pdf", pages=pages, size=len(pdf),
            created=time.time(), source=source,
        )
        self._write_meta(meta)
        return meta

    def get(self, doc_id: str) -> DocumentMeta:
        d = self._dir(doc_id)
        return DocumentMeta(**json.loads((d / "meta.json").read_text()))

    def list(self) -> list[DocumentMeta]:
        docs = []
        for d in self.root.iterdir():
            if d.is_dir() and ID_RE.match(d.name) and (d / "meta.json").is_file():
                try:
                    docs.append(DocumentMeta(**json.loads((d / "meta.json").read_text())))
                except (ValueError, TypeError):
                    continue
        return sorted(docs, key=lambda m: m.created, reverse=True)

    def pdf_path(self, doc_id: str) -> Path:
        return self._dir(doc_id) / "document.pdf"

    def delete(self, doc_id: str) -> None:
        shutil.rmtree(self._dir(doc_id))

    def rename(self, doc_id: str, name: str) -> DocumentMeta:
        meta = self.get(doc_id)
        meta.name = f"{Path(safe_name(name)).stem}.pdf"
        self._write_meta(meta)
        return meta

    def render_page(self, doc_id: str, page: int, width: int = 1000) -> bytes:
        """Render one page (0-based) to PNG at the given pixel width."""
        width = max(100, min(width, 3000))
        path = self.pdf_path(doc_id)
        with self._lock:  # pdfium is not thread-safe
            pdf = pdfium.PdfDocument(str(path))
            try:
                if not 0 <= page < len(pdf):
                    raise NotFound(f"page {page}")
                p = pdf[page]
                scale = width / p.get_width()
                img = p.render(scale=scale).to_pil()
            finally:
                pdf.close()
        out = io.BytesIO()
        img.save(out, "PNG", optimize=False)
        return out.getvalue()

    def page_sizes(self, doc_id: str) -> list[tuple[float, float]]:
        """Visible page sizes in points (rotation applied), as the viewer shows them."""
        with self._lock:
            pdf = pdfium.PdfDocument(str(self.pdf_path(doc_id)))
            try:
                return [tuple(pdf[i].get_size()) for i in range(len(pdf))]
            finally:
                pdf.close()

    def get_annotations(self, doc_id: str) -> list[dict]:
        f = self._dir(doc_id) / "annotations.json"
        return json.loads(f.read_text()) if f.is_file() else []

    def set_annotations(self, doc_id: str, annotations: list[dict]) -> DocumentMeta:
        d = self._dir(doc_id)
        (d / "annotations.json").write_text(json.dumps(annotations))
        meta = self.get(doc_id)
        meta.has_annotations = bool(annotations)
        self._write_meta(meta)
        return meta

    def purge_older_than(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        removed = 0
        for meta in self.list():
            if meta.created < cutoff:
                shutil.rmtree(self.root / meta.id, ignore_errors=True)
                removed += 1
        return removed
