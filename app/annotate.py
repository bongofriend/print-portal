"""Flatten annotations into a PDF.

Annotations use coordinates normalised to the *visible* page (0..1, origin at
the top-left corner, page rotation already applied), which is exactly what the
browser editor sees. Sizes (stroke width, font size) are fractions of the page
width so they are independent of zoom level.

Annotations are burned into the page content (not stored as PDF annotation
objects) so every viewer and every printer driver renders them identically.
"""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_FONT = "Helvetica"
for _candidate in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
):
    if Path(_candidate).is_file():
        pdfmetrics.registerFont(TTFont("DejaVuSans", _candidate))
        _FONT = "DejaVuSans"  # covers far more of Unicode than Helvetica
        break


def _draw(c: canvas.Canvas, a: dict, w: float, h: float) -> None:
    color = HexColor(a.get("color", "#000000"))
    X = lambda v: v * w  # noqa: E731
    Y = lambda v: h - v * h  # noqa: E731  (flip: PDF origin is bottom-left)
    kind = a["type"]
    c.saveState()
    if kind == "pen":
        pts = a["points"]
        c.setStrokeColor(color)
        c.setLineWidth(max(a.get("width", 0.003) * w, 0.1))
        c.setLineCap(1)
        c.setLineJoin(1)
        p = c.beginPath()
        p.moveTo(X(pts[0][0]), Y(pts[0][1]))
        if len(pts) == 1:
            p.lineTo(X(pts[0][0]) + 0.01, Y(pts[0][1]))
        for x, y in pts[1:]:
            p.lineTo(X(x), Y(y))
        c.drawPath(p, stroke=1, fill=0)
    elif kind in ("highlight", "rect", "whiteout"):
        x, y, rw, rh = X(a["x"]), Y(a["y"] + a["h"]), a["w"] * w, a["h"] * h
        if kind == "highlight":
            try:
                c.setBlendMode("Multiply")  # keeps text under the highlight crisp
            except Exception:  # older reportlab
                pass
            c.setFillColor(color)
            c.setFillAlpha(0.4)
            c.rect(x, y, rw, rh, stroke=0, fill=1)
        elif kind == "whiteout":
            c.setFillColor(HexColor("#ffffff"))
            c.rect(x, y, rw, rh, stroke=0, fill=1)
        else:
            c.setStrokeColor(color)
            c.setLineWidth(max(a.get("width", 0.003) * w, 0.1))
            c.rect(x, y, rw, rh, stroke=1, fill=0)
    elif kind == "text":
        size = max(a.get("size", 0.02) * w, 1)
        c.setFillColor(color)
        c.setFont(_FONT, size)
        ascent = pdfmetrics.getAscent(_FONT, size)
        baseline = Y(a["y"]) - ascent
        for i, line in enumerate(str(a.get("text", "")).split("\n")):
            c.drawString(X(a["x"]), baseline - i * size * 1.2, line)
    c.restoreState()


def flatten(pdf_path: Path, annotations: list[dict]) -> bytes:
    """Return the PDF at pdf_path with annotations burned in."""
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter(clone_from=reader)
    by_page: dict[int, list[dict]] = {}
    for a in annotations:
        by_page.setdefault(int(a["page"]), []).append(a)

    for idx, items in by_page.items():
        if not 0 <= idx < len(writer.pages):
            continue
        page = writer.pages[idx]
        # Normalise /Rotate into the content so "visible" coords == user space.
        if page.rotation % 360:
            page.transfer_rotation_to_content()
        box = page.cropbox
        w, h = float(box.width), float(box.height)

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        for a in items:
            _draw(c, a, w, h)
        c.showPage()
        c.save()
        overlay = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        page.merge_transformed_page(
            overlay, Transformation().translate(float(box.left), float(box.bottom)), expand=False
        )

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
