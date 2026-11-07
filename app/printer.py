"""Printer backends.

CupsPrinter shells out to the CUPS command-line client (lp, lpstat, lpoptions,
cancel). Point it at a CUPS server with the standard CUPS_SERVER environment
variable (in Kubernetes this is the cups sidecar on localhost:631).

MockPrinter simulates a printer for local development and tests.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PRINTER_STATES = {3: "idle", 4: "printing", 5: "stopped"}


class PrinterError(RuntimeError):
    pass


@dataclass
class PrintOptions:
    copies: int = 1
    sides: str = "one-sided"  # one-sided | two-sided-long-edge | two-sided-short-edge
    color: str = "color"  # color | monochrome
    media: str = ""  # e.g. A4, Letter; empty = printer default
    page_ranges: str = ""  # e.g. "1-3,5"; empty = all
    orientation: str = ""  # "" (auto) | portrait | landscape
    fit_to_page: bool = True


@dataclass
class Marker:
    name: str
    level: int | None  # percent, None if unknown
    color: str
    type: str


@dataclass
class PrinterStatus:
    name: str
    backend: str
    online: bool
    state: str  # idle | printing | stopped | unknown
    accepting_jobs: bool
    state_reasons: list[str]
    model: str = ""
    location: str = ""
    message: str = ""
    markers: list[Marker] = field(default_factory=list)
    queued_jobs: int = 0


@dataclass
class PrintJob:
    id: str
    title: str
    document_id: str
    pages: int
    options: dict
    submitted: float
    state: str = "pending"  # pending | printing | completed | canceled | unknown


class _JobLog:
    """Persists the portal's view of submitted jobs (CUPS only knows file names)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.jobs: dict[str, PrintJob] = {}
        if path.is_file():
            try:
                for j in json.loads(path.read_text()):
                    self.jobs[j["id"]] = PrintJob(**j)
            except (ValueError, TypeError, KeyError):
                pass

    def save(self) -> None:
        recent = sorted(self.jobs.values(), key=lambda j: j.submitted, reverse=True)[:200]
        self.jobs = {j.id: j for j in recent}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in recent]))
        tmp.replace(self.path)


def options_to_lp_args(opts: PrintOptions) -> list[str]:
    args = ["-n", str(opts.copies), "-o", f"sides={opts.sides}", "-o", f"print-color-mode={opts.color}"]
    if opts.media:
        args += ["-o", f"media={opts.media}"]
    if opts.page_ranges:
        args += ["-P", opts.page_ranges]
    if opts.orientation == "landscape":
        args += ["-o", "orientation-requested=4"]
    elif opts.orientation == "portrait":
        args += ["-o", "orientation-requested=3"]
    if opts.fit_to_page:
        args += ["-o", "fit-to-page"]
    return args


def parse_lpoptions(output: str) -> dict[str, str]:
    """Parse `lpoptions -p NAME` output: space separated key=value, shell-quoted."""
    attrs: dict[str, str] = {}
    try:
        tokens = shlex.split(output)
    except ValueError:
        tokens = output.split()
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            attrs[k] = v
    return attrs


def markers_from_attrs(attrs: dict[str, str]) -> list[Marker]:
    names = [n for n in attrs.get("marker-names", "").split(",") if n]
    levels = attrs.get("marker-levels", "").split(",")
    colors = attrs.get("marker-colors", "").split(",")
    types = attrs.get("marker-types", "").split(",")
    out = []
    for i, name in enumerate(names):
        try:
            lvl = int(levels[i])
            level = lvl if 0 <= lvl <= 100 else None  # -1/-2/-3 mean unknown
        except (IndexError, ValueError):
            level = None
        color = colors[i] if i < len(colors) else ""
        # Multi-colour cartridges are reported as "#00FFFF#FF00FF#FFFF00"; keep the first.
        m = re.match(r"#[0-9A-Fa-f]{6}", color)
        out.append(Marker(name=name, level=level, color=m.group(0) if m else "#888888",
                          type=types[i] if i < len(types) else ""))
    return out


def parse_lpstat_jobs(output: str) -> list[str]:
    """Job ids from `lpstat -o` output lines like 'office-12  alice  1024  <date>'."""
    return [line.split()[0] for line in output.splitlines() if line.strip()]


class CupsPrinter:
    backend = "cups"

    def __init__(self, name: str, state_dir: Path):
        self.name = name
        self.log = _JobLog(state_dir / "print-jobs.json")

    def _run(self, *args: str, timeout: int = 15) -> str:
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise PrinterError(f"{args[0]} not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise PrinterError(f"{args[0]} timed out (is the CUPS server reachable?)") from exc
        if res.returncode != 0:
            raise PrinterError((res.stderr or res.stdout).strip() or f"{args[0]} failed")
        return res.stdout

    def status(self) -> PrinterStatus:
        try:
            attrs = parse_lpoptions(self._run("lpoptions", "-p", self.name))
        except PrinterError as exc:
            return PrinterStatus(name=self.name, backend=self.backend, online=False, state="unknown",
                                 accepting_jobs=False, state_reasons=[], message=str(exc))
        reasons = [r for r in attrs.get("printer-state-reasons", "none").split(",") if r and r != "none"]
        try:
            state = PRINTER_STATES.get(int(attrs.get("printer-state", "0")), "unknown")
        except ValueError:
            state = "unknown"
        try:
            queued = len(parse_lpstat_jobs(self._run("lpstat", "-W", "not-completed", "-o", self.name)))
        except PrinterError:
            queued = 0
        offline = any(r.startswith("offline") for r in reasons)
        return PrinterStatus(
            name=self.name, backend=self.backend, online=not offline, state=state,
            accepting_jobs=attrs.get("printer-is-accepting-jobs", "true") == "true",
            state_reasons=reasons, model=attrs.get("printer-make-and-model", ""),
            location=attrs.get("printer-location", ""), message=attrs.get("printer-state-message", ""),
            markers=markers_from_attrs(attrs), queued_jobs=queued,
        )

    def submit(self, pdf: Path, title: str, document_id: str, pages: int, opts: PrintOptions) -> PrintJob:
        out = self._run("lp", "-d", self.name, "-t", title, *options_to_lp_args(opts), "--", str(pdf))
        m = re.search(r"request id is (\S+)", out)
        if not m:
            raise PrinterError(f"Unexpected lp output: {out.strip()}")
        job = PrintJob(id=m.group(1), title=title, document_id=document_id, pages=pages,
                       options=asdict(opts), submitted=time.time())
        with self.log.lock:
            self.log.jobs[job.id] = job
            self.log.save()
        return job

    def jobs(self) -> list[PrintJob]:
        try:
            active = parse_lpstat_jobs(self._run("lpstat", "-W", "not-completed", "-o", self.name))
            done = set(parse_lpstat_jobs(self._run("lpstat", "-W", "completed", "-o", self.name)))
        except PrinterError:
            active, done = None, set()
        with self.log.lock:
            for job in self.log.jobs.values():
                if job.state == "canceled" or active is None:
                    continue
                if job.id in active:
                    job.state = "printing" if active.index(job.id) == 0 else "pending"
                elif job.id in done:
                    job.state = "completed"
                elif job.state in ("pending", "printing"):
                    job.state = "completed"  # purged from CUPS history
            self.log.save()
            return sorted(self.log.jobs.values(), key=lambda j: j.submitted, reverse=True)

    def cancel(self, job_id: str) -> None:
        if not re.fullmatch(r"[\w.\-]+-\d+", job_id):
            raise PrinterError("invalid job id")
        self._run("cancel", job_id)
        with self.log.lock:
            if job_id in self.log.jobs:
                self.log.jobs[job_id].state = "canceled"
                self.log.save()


class MockPrinter:
    """Simulated printer: each page takes `seconds_per_page` to print."""

    backend = "mock"

    def __init__(self, name: str, state_dir: Path, seconds_per_page: float = 3.0):
        self.name = name
        self.seconds_per_page = seconds_per_page
        self.log = _JobLog(state_dir / "print-jobs.json")
        self._counter = int(time.time()) % 10000
        self._levels = {"Black Toner": 72, "Cyan Toner": 45, "Magenta Toner": 18, "Yellow Toner": 90}
        self._colors = {"Black Toner": "#000000", "Cyan Toner": "#00FFFF",
                        "Magenta Toner": "#FF00FF", "Yellow Toner": "#FFFF00"}

    def _advance(self) -> None:
        now = time.time()
        t = None  # time the queue head becomes free
        for job in sorted(self.log.jobs.values(), key=lambda j: j.submitted):
            if job.state not in ("pending", "printing"):
                continue
            start = max(job.submitted, t or job.submitted)
            end = start + self.seconds_per_page * max(job.pages, 1) * job.options.get("copies", 1)
            if now >= end:
                job.state = "completed"
            elif now >= start:
                job.state = "printing"
            else:
                job.state = "pending"
            t = end

    def status(self) -> PrinterStatus:
        with self.log.lock:
            self._advance()
            active = [j for j in self.log.jobs.values() if j.state in ("pending", "printing")]
        reasons = ["marker-supply-low-warning"] if min(self._levels.values()) < 20 else []
        return PrinterStatus(
            name=self.name, backend=self.backend, online=True,
            state="printing" if active else "idle", accepting_jobs=True, state_reasons=reasons,
            model="Simulated Laser MFP", location="Development", message="",
            markers=[Marker(n, lvl, self._colors[n], "toner") for n, lvl in self._levels.items()],
            queued_jobs=len(active),
        )

    def submit(self, pdf: Path, title: str, document_id: str, pages: int, opts: PrintOptions) -> PrintJob:
        with self.log.lock:
            self._counter += 1
            job = PrintJob(id=f"{self.name}-{self._counter}", title=title, document_id=document_id,
                           pages=pages, options=asdict(opts), submitted=time.time())
            self.log.jobs[job.id] = job
            self.log.save()
        return job

    def jobs(self) -> list[PrintJob]:
        with self.log.lock:
            self._advance()
            self.log.save()
            return sorted(self.log.jobs.values(), key=lambda j: j.submitted, reverse=True)

    def cancel(self, job_id: str) -> None:
        with self.log.lock:
            job = self.log.jobs.get(job_id)
            if not job or job.state not in ("pending", "printing"):
                raise PrinterError("job not found or already finished")
            job.state = "canceled"
            self.log.save()


def make_printer(backend: str, name: str, state_dir: Path):
    if backend == "mock":
        return MockPrinter(name, state_dir)
    if backend == "cups":
        return CupsPrinter(name, state_dir)
    raise ValueError(f"unknown PRINTER_BACKEND {backend!r}")
