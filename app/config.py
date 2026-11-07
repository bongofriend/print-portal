"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))
    # "cups" talks to a CUPS server (CUPS_SERVER env is honoured by the CUPS client tools);
    # "mock" simulates a printer, useful for development and tests.
    printer_backend: str = field(default_factory=lambda: os.environ.get("PRINTER_BACKEND", "cups"))
    printer_name: str = field(default_factory=lambda: os.environ.get("PRINTER_NAME", "office"))
    # "sane" shells out to scanimage; "mock" generates a test page.
    scanner_backend: str = field(default_factory=lambda: os.environ.get("SCANNER_BACKEND", "sane"))
    # SANE device name, e.g. "airscan:e0:My Printer". Empty = first device found.
    scanner_device: str = field(default_factory=lambda: os.environ.get("SCANNER_DEVICE", ""))
    scan_timeout: int = field(default_factory=lambda: int(os.environ.get("SCAN_TIMEOUT_SECONDS", "300")))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("MAX_UPLOAD_MB", "50")))
    # Optional HTTP basic auth. Leave unset when auth is handled by the ingress / an auth proxy.
    auth_user: str = field(default_factory=lambda: os.environ.get("PORTAL_USER", ""))
    auth_password: str = field(default_factory=lambda: os.environ.get("PORTAL_PASSWORD", ""))
    retention_days: int = field(default_factory=lambda: int(os.environ.get("RETENTION_DAYS", "30")))

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def scans_dir(self) -> Path:
        return self.data_dir / "scans"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    def ensure_dirs(self) -> None:
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.scans_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s

