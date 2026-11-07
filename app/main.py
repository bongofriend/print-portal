"""Print portal HTTP API + static web UI."""
from __future__ import annotations

import base64
import binascii
import logging
import secrets
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal, Union

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import annotate
from .config import Settings, load_settings
from .documents import DocumentError, DocumentStore, NotFound
from .printer import PrinterError, PrintOptions, make_printer
from .scanner import ScanError, ScanManager, ScanOptions, make_scanner

log = logging.getLogger("print-portal")
STATIC = Path(__file__).parent / "static"

# --------------------------------------------------------------------------- models
Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Unit = Annotated[float, Field(ge=-0.1, le=1.1)]  # allow slight overshoot at page edges
Size = Annotated[float, Field(gt=0, le=1)]


class _Base(BaseModel):
    page: int = Field(ge=0)
    color: Color = "#000000"


class PenAnnotation(_Base):
    type: Literal["pen"]
    width: Size = 0.003
    points: list[tuple[Unit, Unit]] = Field(min_length=1, max_length=10000)


class BoxAnnotation(_Base):
    type: Literal["highlight", "rect", "whiteout"]
    x: Unit
    y: Unit
    w: Size
    h: Size
    width: Size = 0.003


class TextAnnotation(_Base):
    type: Literal["text"]
    x: Unit
    y: Unit
    size: Size = 0.02
    text: str = Field(min_length=1, max_length=5000)


AnyAnnotation = Annotated[Union[PenAnnotation, BoxAnnotation, TextAnnotation], Field(discriminator="type")]


class AnnotationsBody(BaseModel):
    annotations: list[AnyAnnotation] = Field(max_length=5000)


class PrintOptionsBody(BaseModel):
    copies: int = Field(1, ge=1, le=99)
    sides: Literal["one-sided", "two-sided-long-edge", "two-sided-short-edge"] = "one-sided"
    color: Literal["color", "monochrome"] = "color"
    media: Literal["", "A4", "A3", "A5", "Letter", "Legal"] = ""
    page_ranges: str = Field("", pattern=r"^(\s*\d+(\s*-\s*\d+)?\s*)(,\s*\d+(\s*-\s*\d+)?\s*)*$|^$")
    orientation: Literal["", "portrait", "landscape"] = ""
    fit_to_page: bool = True

    @field_validator("page_ranges")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.replace(" ", "")


class PrintBody(BaseModel):
    document_id: str
    include_annotations: bool = True
    options: PrintOptionsBody = PrintOptionsBody()


class ScanBody(BaseModel):
    resolution: Literal[75, 150, 200, 300, 600] = 300
    mode: Literal["Color", "Gray", "Lineart"] = "Color"
    source: Literal["Flatbed", "ADF", "ADF Duplex"] = "Flatbed"
    format: Literal["pdf", "png", "jpeg"] = "pdf"


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


# --------------------------------------------------------------------------- app
def create_app(settings: Settings | None = None, scanner=None, printer=None) -> FastAPI:
    """App factory. Run with: uvicorn --factory app.main:create_app"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or load_settings()
    settings.ensure_dirs()
    docs = DocumentStore(settings.documents_dir)
    printer = printer or make_printer(settings.printer_backend, settings.printer_name, settings.data_dir)
    scans = ScanManager(
        scanner or make_scanner(settings.scanner_backend, settings.scanner_device, settings.scan_timeout),
        settings.scans_dir,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        threading.Thread(target=purge_loop, daemon=True).start()
        yield

    app = FastAPI(title="Print Portal", version="1.0.0", docs_url="/api/docs",
                  openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.settings, app.state.docs, app.state.printer, app.state.scans = settings, docs, printer, scans

    # -- housekeeping ----------------------------------------------------------
    def purge() -> None:
        age = settings.retention_days * 86400
        if age > 0:
            n = docs.purge_older_than(age) + scans.purge_older_than(age)
            if n:
                log.info("retention: removed %d old documents/scans", n)

    def purge_loop() -> None:
        while True:
            try:
                purge()
            except Exception:  # never let housekeeping kill the thread
                log.exception("retention purge failed")
            time.sleep(3600)

    # -- auth --------------------------------------------------------------------
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if settings.auth_enabled and request.url.path not in ("/healthz", "/readyz"):
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user.encode(), settings.auth_user.encode()) & \
                        secrets.compare_digest(pw.encode(), settings.auth_password.encode())
                except (binascii.Error, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Print Portal"'})
        return await call_next(request)

    # -- health ------------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        if not settings.data_dir.is_dir():
            return JSONResponse({"ok": False, "error": "data dir missing"}, status_code=503)
        return {"ok": True}

    # -- status ------------------------------------------------------------------
    @app.get("/api/status")
    def status():
        return {
            "printer": asdict(printer.status()),
            "scanner": {**scans.scanner.status(), "busy": scans.busy},
        }

    # -- documents ---------------------------------------------------------------
    def _doc_or_404(doc_id: str):
        try:
            return docs.get(doc_id)
        except NotFound:
            raise HTTPException(404, "Document not found") from None

    @app.get("/api/documents")
    def list_documents():
        return [asdict(d) for d in docs.list()]

    @app.post("/api/documents", status_code=201)
    async def upload_document(file: UploadFile = File(...)):
        limit = settings.max_upload_mb * 1024 * 1024
        chunks, total = [], 0
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
            chunks.append(chunk)
        if not total:
            raise HTTPException(400, "Empty file")
        try:
            meta = docs.add(file.filename or "document", b"".join(chunks))
        except DocumentError as exc:
            raise HTTPException(415, str(exc)) from None
        return asdict(meta)

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: str):
        meta = _doc_or_404(doc_id)
        return {**asdict(meta), "page_sizes": docs.page_sizes(doc_id)}

    @app.patch("/api/documents/{doc_id}")
    def rename_document(doc_id: str, body: RenameBody):
        _doc_or_404(doc_id)
        return asdict(docs.rename(doc_id, body.name))

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str):
        _doc_or_404(doc_id)
        docs.delete(doc_id)
        return Response(status_code=204)

    @app.get("/api/documents/{doc_id}/download")
    def download_document(doc_id: str, annotated: bool = True):
        meta = _doc_or_404(doc_id)
        anns = docs.get_annotations(doc_id) if annotated else []
        disposition = {"Content-Disposition": f'attachment; filename="{meta.name}"'}
        if anns:
            data = annotate.flatten(docs.pdf_path(doc_id), anns)
            return Response(data, media_type="application/pdf", headers=disposition)
        return FileResponse(docs.pdf_path(doc_id), media_type="application/pdf", headers=disposition)

    @app.get("/api/documents/{doc_id}/pages/{page}")
    def render_page(doc_id: str, page: int, width: int = Query(1000, ge=100, le=3000)):
        _doc_or_404(doc_id)
        try:
            png = docs.render_page(doc_id, page, width)
        except NotFound:
            raise HTTPException(404, "Page not found") from None
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/annotations")
    def get_annotations(doc_id: str):
        _doc_or_404(doc_id)
        return {"annotations": docs.get_annotations(doc_id)}

    @app.put("/api/documents/{doc_id}/annotations")
    def put_annotations(doc_id: str, body: AnnotationsBody):
        meta = _doc_or_404(doc_id)
        anns = [a.model_dump() for a in body.annotations]
        if any(a["page"] >= meta.pages for a in anns):
            raise HTTPException(422, "Annotation references a page that does not exist")
        docs.set_annotations(doc_id, anns)
        return {"annotations": anns}

    # -- printing ----------------------------------------------------------------
    @app.post("/api/print", status_code=201)
    def print_document(body: PrintBody):
        meta = _doc_or_404(body.document_id)
        anns = docs.get_annotations(meta.id) if body.include_annotations else []
        opts = PrintOptions(**body.options.model_dump())
        try:
            if anns:
                with tempfile.NamedTemporaryFile(suffix=".pdf", dir=settings.data_dir) as tmp:
                    tmp.write(annotate.flatten(docs.pdf_path(meta.id), anns))
                    tmp.flush()
                    job = printer.submit(Path(tmp.name), meta.name, meta.id, meta.pages, opts)
            else:
                job = printer.submit(docs.pdf_path(meta.id), meta.name, meta.id, meta.pages, opts)
        except PrinterError as exc:
            raise HTTPException(502, f"Printer error: {exc}") from None
        return asdict(job)

    @app.get("/api/jobs")
    def list_jobs():
        return [asdict(j) for j in printer.jobs()]

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def cancel_job(job_id: str):
        try:
            printer.cancel(job_id)
        except PrinterError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    # -- scanning ----------------------------------------------------------------
    def _scan_or_404(scan_id: str):
        try:
            return scans.get(scan_id)
        except KeyError:
            raise HTTPException(404, "Scan not found") from None

    def _scan_json(s):
        return {**asdict(s), "media_type": s.media_type}

    @app.get("/api/scans")
    def list_scans():
        return [_scan_json(s) for s in scans.list()]

    @app.post("/api/scans", status_code=202)
    def start_scan(body: ScanBody):
        try:
            return _scan_json(scans.start(ScanOptions(**body.model_dump())))
        except ScanError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/scans/{scan_id}")
    def get_scan(scan_id: str):
        return _scan_json(_scan_or_404(scan_id))

    @app.get("/api/scans/{scan_id}/download")
    def download_scan(scan_id: str, inline: bool = False):
        s = _scan_or_404(scan_id)
        if s.state != "done":
            raise HTTPException(409, "Scan not finished")
        ext = s.options["format"]
        name = f"{s.name}.{'jpg' if ext == 'jpeg' else ext}"
        kind = "inline" if inline else "attachment"
        return FileResponse(scans.file(s.id), media_type=s.media_type,
                            headers={"Content-Disposition": f'{kind}; filename="{name}"'})

    @app.post("/api/scans/{scan_id}/to-document", status_code=201)
    def scan_to_document(scan_id: str):
        s = _scan_or_404(scan_id)
        if s.state != "done":
            raise HTTPException(409, "Scan not finished")
        meta = docs.add(s.name, scans.file(s.id).read_bytes(), source="scan")
        return asdict(meta)

    @app.delete("/api/scans/{scan_id}", status_code=204)
    def delete_scan(scan_id: str):
        _scan_or_404(scan_id)
        try:
            scans.delete(scan_id)
        except ScanError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    # -- UI ------------------------------------------------------------------------
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app

