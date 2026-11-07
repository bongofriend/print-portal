"""Scanner backends and scan job management.

SaneScanner shells out to `scanimage` (sane-utils). In the container the
sane-airscan backend is installed, which talks eSCL/WSD to almost every
network MFP made in the last decade; configure the device in
/etc/sane.d/airscan.conf (see k8s/configmap.yaml).

Scans run in a background thread (one at a time - a scanner is a single
physical device); the API returns a scan id that the UI polls.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

ID_RE = re.compile(r"^[0-9a-f]{32}$")
MEDIA_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}


class ScanError(RuntimeError):
    pass


@dataclass
class ScanOptions:
    resolution: int = 300
    mode: str = "Color"  # Color | Gray | Lineart
    source: str = "Flatbed"  # Flatbed | ADF | ADF Duplex
    format: str = "pdf"  # pdf | png | jpeg (png/jpeg: first page only)


@dataclass
class Scan:
    id: str
    name: str
    created: float
    options: dict
    state: str = "scanning"  # scanning | done | failed
    error: str = ""
    pages: int = 0
    size: int = 0
    finished: float | None = None

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.options["format"]]

    @property
    def filename(self) -> str:
        return f"scan.{self.options['format']}"


def images_to_output(images: list[Image.Image], fmt: str, resolution: int) -> bytes:
    out = io.BytesIO()
    if fmt == "pdf":
        rgb = [im.convert("RGB") if im.mode not in ("RGB", "L", "1") else im for im in images]
        rgb[0].save(out, "PDF", save_all=True, append_images=rgb[1:], resolution=float(resolution))
    elif fmt == "png":
        images[0].save(out, "PNG")
    else:
        images[0].convert("RGB").save(out, "JPEG", quality=90, dpi=(resolution, resolution))
    return out.getvalue()


class SaneScanner:
    backend = "sane"

    def __init__(self, device: str = "", timeout: int = 300, status_ttl: float = 60.0):
        self.device = device
        self.timeout = timeout
        self.status_ttl = status_ttl  # device discovery is slow; cache it
        self._status_cache: tuple[float, dict] | None = None

    def devices(self) -> list[dict]:
        try:
            res = subprocess.run(["scanimage", "-f", "%d\t%v %m%n"], capture_output=True,
                                 text=True, timeout=30, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ScanError(f"scanimage unavailable: {exc}") from exc
        devs = []
        for line in res.stdout.splitlines():
            if "\t" in line:
                dev, desc = line.split("\t", 1)
                devs.append({"device": dev, "description": desc.strip()})
        return devs

    def status(self) -> dict:
        if self._status_cache and time.time() - self._status_cache[0] < self.status_ttl:
            return self._status_cache[1]
        result = self._probe()
        self._status_cache = (time.time(), result)
        return result

    def _probe(self) -> dict:
        try:
            devs = self.devices()
        except ScanError as exc:
            return {"backend": self.backend, "available": False, "device": self.device, "message": str(exc)}
        if self.device:
            ok = any(d["device"] == self.device for d in devs)
        else:
            ok = bool(devs)
        return {"backend": self.backend, "available": ok,
                "device": self.device or (devs[0]["device"] if devs else ""),
                "message": "" if ok else "No scanner found", "devices": devs}

    def scan(self, opts: ScanOptions) -> list[Image.Image]:
        base = ["scanimage", "--format=png", f"--resolution={opts.resolution}", f"--mode={opts.mode}"]
        if self.device:
            base += ["-d", self.device]
        if opts.source:
            base += [f"--source={opts.source}"]
        tmp = Path(tempfile.mkdtemp(prefix="scan-"))
        try:
            if opts.source.startswith("ADF"):
                cmd = base + [f"--batch={tmp}/page-%04d.png"]
            else:
                cmd = base + ["-o", str(tmp / "page-0001.png")]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ScanError("Scan timed out") from exc
            except FileNotFoundError as exc:
                raise ScanError("scanimage not installed") from exc
            files = sorted(tmp.glob("page-*.png"))
            # In batch mode scanimage exits non-zero with "Document feeder out of documents"
            # after the last page; that is success as long as we got pages.
            if not files:
                msg = (res.stderr or "").strip().splitlines()
                raise ScanError(msg[-1] if msg else f"scanimage exited with {res.returncode}")
            images = []
            for f in files:
                with Image.open(f) as im:
                    im.load()
                    images.append(im)
            return images
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MockScanner:
    backend = "mock"

    def __init__(self, delay: float = 2.0):
        self.delay = delay

    def status(self) -> dict:
        return {"backend": self.backend, "available": True, "device": "mock:scanner",
                "message": "", "devices": [{"device": "mock:scanner", "description": "Simulated scanner"}]}

    def scan(self, opts: ScanOptions) -> list[Image.Image]:
        time.sleep(self.delay)
        pages = 2 if opts.source.startswith("ADF") else 1
        w, h = int(8.27 * opts.resolution / 4), int(11.69 * opts.resolution / 4)  # A4 at 1/4 res
        out = []
        for n in range(pages):
            im = Image.new("RGB", (w, h), "white")
            d = ImageDraw.Draw(im)
            d.rectangle([20, 20, w - 20, h - 20], outline="black", width=3)
            d.text((40, 40), f"Simulated scan - page {n + 1}", fill="black")
            d.text((40, 60), time.strftime("%Y-%m-%d %H:%M:%S"), fill="gray")
            d.ellipse([w // 4, h // 3, 3 * w // 4, h // 3 + w // 2], outline="navy", width=4)
            if opts.mode == "Gray":
                im = im.convert("L")
            elif opts.mode == "Lineart":
                im = im.convert("1")
            out.append(im)
        return out


class ScanManager:
    """Runs scans in the background and stores results under scans/<id>/."""

    def __init__(self, scanner, root: Path):
        self.scanner = scanner
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._device_lock = threading.Lock()
        self._lock = threading.Lock()
        self._scans: dict[str, Scan] = {}
        for d in root.iterdir():
            meta = d / "meta.json"
            if d.is_dir() and ID_RE.match(d.name) and meta.is_file():
                try:
                    s = Scan(**json.loads(meta.read_text()))
                except (ValueError, TypeError):
                    continue
                if s.state == "scanning":  # interrupted by a restart
                    s.state, s.error = "failed", "Interrupted"
                self._scans[s.id] = s

    def _save(self, s: Scan) -> None:
        d = self.root / s.id
        d.mkdir(exist_ok=True)
        tmp = d / "meta.tmp"
        tmp.write_text(json.dumps(asdict(s)))
        tmp.replace(d / "meta.json")

    @property
    def busy(self) -> bool:
        return self._device_lock.locked()

    def start(self, opts: ScanOptions) -> Scan:
        if not self._device_lock.acquire(blocking=False):
            raise ScanError("The scanner is busy with another scan")
        scan = Scan(id=uuid.uuid4().hex, name=time.strftime("Scan %Y-%m-%d %H.%M.%S"),
                    created=time.time(), options=asdict(opts))
        with self._lock:
            self._scans[scan.id] = scan
            self._save(scan)
        threading.Thread(target=self._run, args=(scan, opts), daemon=True).start()
        return scan

    def _run(self, scan: Scan, opts: ScanOptions) -> None:
        try:
            images = self.scanner.scan(opts)
            data = images_to_output(images, opts.format, opts.resolution)
            (self.root / scan.id / scan.filename).write_bytes(data)
            scan.pages, scan.size, scan.state = len(images), len(data), "done"
        except Exception as exc:  # report any failure to the UI
            scan.state, scan.error = "failed", str(exc) or exc.__class__.__name__
        finally:
            scan.finished = time.time()
            with self._lock:
                self._save(scan)
            self._device_lock.release()

    def list(self) -> list[Scan]:
        with self._lock:
            return sorted(self._scans.values(), key=lambda s: s.created, reverse=True)

    def get(self, scan_id: str) -> Scan:
        with self._lock:
            s = self._scans.get(scan_id) if ID_RE.match(scan_id or "") else None
        if not s:
            raise KeyError(scan_id)
        return s

    def file(self, scan_id: str) -> Path:
        s = self.get(scan_id)
        if s.state != "done":
            raise KeyError(scan_id)
        return self.root / s.id / s.filename

    def delete(self, scan_id: str) -> None:
        s = self.get(scan_id)
        if s.state == "scanning":
            raise ScanError("Scan still in progress")
        with self._lock:
            self._scans.pop(s.id, None)
        shutil.rmtree(self.root / s.id, ignore_errors=True)

    def purge_older_than(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        old = [s.id for s in self.list() if s.created < cutoff and s.state != "scanning"]
        for sid in old:
            self.delete(sid)
        return len(old)


def make_scanner(backend: str, device: str, timeout: int):
    if backend == "mock":
        return MockScanner()
    if backend == "sane":
        return SaneScanner(device, timeout)
    raise ValueError(f"unknown SCANNER_BACKEND {backend!r}")
