import base64
import io
import time

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.main import create_app
from app.printer import (MockPrinter, PrintOptions, markers_from_attrs, options_to_lp_args,
                         parse_lpoptions, parse_lpstat_jobs)
from app.scanner import MockScanner


def make_pdf(pages=2, rotate=0, size=(595, 842)) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        p = w.add_blank_page(*size)
        if rotate:
            p.rotate(rotate)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_png(color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 300), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, printer_backend="mock", scanner_backend="mock")


@pytest.fixture
def client(settings):
    app = create_app(settings, scanner=MockScanner(delay=0.05),
                     printer=MockPrinter("office", settings.data_dir, seconds_per_page=0.05))
    with TestClient(app) as c:
        yield c


def upload(client, data, name="test.pdf"):
    r = client.post("/api/documents", files={"file": (name, data)})
    assert r.status_code == 201, r.text
    return r.json()


def pixel(pdf_bytes, page, x, y, width=400):
    """RGB of the rendered page at normalised (x, y)."""
    doc = pdfium.PdfDocument(pdf_bytes)
    p = doc[page]
    img = p.render(scale=width / p.get_width()).to_pil().convert("RGB")
    return img.getpixel((int(x * img.width), int(y * img.height)))


def test_health_and_ui(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").status_code == 200
    r = client.get("/")
    assert r.status_code == 200 and "Print Portal" in r.text


def test_status(client):
    s = client.get("/api/status").json()
    assert s["printer"]["state"] == "idle"
    assert s["printer"]["markers"]
    assert s["scanner"]["available"] is True


def test_upload_pdf_and_image(client):
    d = upload(client, make_pdf(3), "../../etc/passwd report.pdf")
    assert d["pages"] == 3 and d["name"] == "passwd report.pdf"
    img = upload(client, make_png(), "photo.png")
    assert img["pages"] == 1 and img["name"] == "photo.pdf"
    names = [x["name"] for x in client.get("/api/documents").json()]
    assert set(names) == {"passwd report.pdf", "photo.pdf"}
    r = client.get(f"/api/documents/{d['id']}/download")
    assert r.content.startswith(b"%PDF-")


def test_upload_rejects_garbage_and_large(client, settings):
    r = client.post("/api/documents", files={"file": ("x.exe", b"MZ\x90\x00garbage")})
    assert r.status_code == 415
    settings.max_upload_mb = 1
    r = client.post("/api/documents", files={"file": ("big.pdf", b"%PDF-" + b"0" * (1024 * 1024 + 10))})
    assert r.status_code == 413


def test_invalid_ids_are_404(client):
    assert client.get("/api/documents/../../etc/passwd").status_code == 404
    assert client.get("/api/documents/" + "a" * 32).status_code == 404
    assert client.get("/api/scans/nothex").status_code == 404


def test_render_page(client):
    d = upload(client, make_pdf(2))
    r = client.get(f"/api/documents/{d['id']}/pages/1?width=300")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).width == 300
    assert client.get(f"/api/documents/{d['id']}/pages/5").status_code == 404


def test_annotations_roundtrip_and_flatten(client):
    d = upload(client, make_pdf(2))
    anns = [
        {"type": "rect", "page": 0, "color": "#0000ff", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.2, "width": 0.02},
        {"type": "whiteout", "page": 1, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2},
        {"type": "highlight", "page": 1, "color": "#ffff00", "x": 0.0, "y": 0.0, "w": 0.4, "h": 0.1},
        {"type": "pen", "page": 0, "color": "#ff0000", "width": 0.02, "points": [[0.6, 0.8], [0.9, 0.8]]},
        {"type": "text", "page": 1, "color": "#000000", "x": 0.1, "y": 0.8, "size": 0.05, "text": "Hello\nWorld ✓"},
    ]
    r = client.put(f"/api/documents/{d['id']}/annotations", json={"annotations": anns})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/documents/{d['id']}/annotations").json()["annotations"][0]["type"] == "rect"
    assert client.get("/api/documents").json()[0]["has_annotations"] is True

    pdf = client.get(f"/api/documents/{d['id']}/download").content
    # rect stroke on page 0 at the left edge of the box (x=0.1) is blue
    r_, g_, b_ = pixel(pdf, 0, 0.1, 0.2)
    assert b_ > 200 and r_ < 80
    # pen stroke at (0.75, 0.8) is red
    r_, g_, b_ = pixel(pdf, 0, 0.75, 0.8)
    assert r_ > 200 and g_ < 80
    # highlight on page 1 is yellow-ish
    r_, g_, b_ = pixel(pdf, 1, 0.2, 0.05)
    assert r_ > 200 and g_ > 200 and b_ < 200
    # original download is untouched
    orig = client.get(f"/api/documents/{d['id']}/download?annotated=false").content
    assert pixel(orig, 0, 0.75, 0.8) == (255, 255, 255)


def test_flatten_handles_rotated_pages(client):
    # a portrait page rotated 90 degrees is *displayed* landscape
    d = upload(client, make_pdf(1, rotate=90))
    info = client.get(f"/api/documents/{d['id']}").json()
    w, h = info["page_sizes"][0]
    assert w > h
    anns = [{"type": "rect", "page": 0, "color": "#ff0000", "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2, "width": 0.01},
            {"type": "highlight", "page": 0, "color": "#00ff00", "x": 0.0, "y": 0.0, "w": 0.15, "h": 0.15}]
    client.put(f"/api/documents/{d['id']}/annotations", json={"annotations": anns})
    pdf = client.get(f"/api/documents/{d['id']}/download").content
    # the highlight must appear in the visual top-left corner
    r_, g_, b_ = pixel(pdf, 0, 0.07, 0.07)
    assert g_ > 200 and r_ < 200 and b_ < 200, (r_, g_, b_)
    assert pixel(pdf, 0, 0.9, 0.9) == (255, 255, 255)


def test_annotation_validation(client):
    d = upload(client, make_pdf(1))
    bad = [
        [{"type": "rect", "page": 0, "color": "red", "x": 0, "y": 0, "w": 0.1, "h": 0.1}],
        [{"type": "script", "page": 0}],
        [{"type": "pen", "page": 0, "points": []}],
        [{"type": "rect", "page": 3, "x": 0, "y": 0, "w": 0.1, "h": 0.1}],
    ]
    for anns in bad:
        r = client.put(f"/api/documents/{d['id']}/annotations", json={"annotations": anns})
        assert r.status_code == 422, anns


def test_print_flow(client):
    d = upload(client, make_pdf(1))
    r = client.post("/api/print", json={"document_id": d["id"], "options": {"copies": 2, "sides": "two-sided-long-edge"}})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["state"] == "pending" and job["options"]["copies"] == 2
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["id"] == job["id"]
    time.sleep(0.2)
    assert client.get("/api/jobs").json()[0]["state"] == "completed"
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409  # already done


def test_print_cancel_and_validation(client):
    d = upload(client, make_pdf(50))
    job = client.post("/api/print", json={"document_id": d["id"]}).json()
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get("/api/jobs").json()[0]["state"] == "canceled"
    assert client.post("/api/print", json={"document_id": d["id"], "options": {"copies": 0}}).status_code == 422
    assert client.post("/api/print", json={"document_id": d["id"], "options": {"page_ranges": "1;rm -rf"}}).status_code == 422
    ok = client.post("/api/print", json={"document_id": d["id"], "options": {"page_ranges": "1-3, 5"}})
    assert ok.status_code == 201 and ok.json()["options"]["page_ranges"] == "1-3,5"
    assert client.post("/api/print", json={"document_id": "0" * 32}).status_code == 404


def test_scan_flow(client):
    r = client.post("/api/scans", json={"source": "ADF", "mode": "Gray", "resolution": 150, "format": "pdf"})
    assert r.status_code == 202
    sid = r.json()["id"]
    for _ in range(100):
        s = client.get(f"/api/scans/{sid}").json()
        if s["state"] != "scanning":
            break
        time.sleep(0.05)
    assert s["state"] == "done" and s["pages"] == 2, s
    dl = client.get(f"/api/scans/{sid}/download")
    assert dl.headers["content-type"] == "application/pdf"
    assert len(PdfReader(io.BytesIO(dl.content)).pages) == 2
    doc = client.post(f"/api/scans/{sid}/to-document").json()
    assert doc["source"] == "scan" and doc["pages"] == 2
    assert client.delete(f"/api/scans/{sid}").status_code == 204
    assert client.get(f"/api/scans/{sid}").status_code == 404


def test_scan_png_and_busy(client, settings):
    r1 = client.post("/api/scans", json={"format": "png"})
    r2 = client.post("/api/scans", json={"format": "png"})
    assert r1.status_code == 202 and r2.status_code == 409  # scanner busy
    time.sleep(0.3)
    dl = client.get(f"/api/scans/{r1.json()['id']}/download")
    assert dl.headers["content-type"] == "image/png"


def test_basic_auth(tmp_path):
    s = Settings(data_dir=tmp_path, printer_backend="mock", scanner_backend="mock",
                 auth_user="admin", auth_password="s3cret")
    with TestClient(create_app(s, scanner=MockScanner(delay=0))) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/status").status_code == 401
        bad = base64.b64encode(b"admin:wrong").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        good = base64.b64encode(b"admin:s3cret").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {good}"}).status_code == 200


# ---------------------------------------------------------------- CUPS parsing
LPOPTIONS = (
    "copies=1 device-uri=ipp://10.0.0.5/ipp/print finishings=3 marker-colors=#000000,#00FFFF,#FF00FF#FFFF00 "
    "marker-levels=64,-1,12 marker-names='Black Toner',Cyan,'Tri Color' marker-types=toner,toner,ink "
    "printer-info='Office MFP' printer-is-accepting-jobs=true printer-location='2nd floor' "
    "printer-make-and-model='Brother MFC-L8900CDW' printer-state=4 printer-state-reasons=toner-low-warning"
)


def test_parse_lpoptions_and_markers():
    attrs = parse_lpoptions(LPOPTIONS)
    assert attrs["printer-location"] == "2nd floor"
    assert attrs["printer-state"] == "4"
    m = markers_from_attrs(attrs)
    assert [x.name for x in m] == ["Black Toner", "Cyan", "Tri Color"]
    assert [x.level for x in m] == [64, None, 12]
    assert m[2].color == "#FF00FF"


def test_parse_lpstat_and_lp_args():
    out = "office-12   alice   1024   Sat 07 Nov 2026 01:00:00 PM UTC\noffice-13  bob  2048  Sat\n"
    assert parse_lpstat_jobs(out) == ["office-12", "office-13"]
    args = options_to_lp_args(PrintOptions(copies=2, sides="two-sided-long-edge", color="monochrome",
                                           media="A4", page_ranges="1-2", orientation="landscape"))
    assert args == ["-n", "2", "-o", "sides=two-sided-long-edge", "-o", "print-color-mode=monochrome",
                    "-o", "media=A4", "-P", "1-2", "-o", "orientation-requested=4", "-o", "fit-to-page"]
