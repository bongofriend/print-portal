"""Development only: a fake network printer speaking the raw "JetDirect"
protocol (TCP 9100). Every job received is saved to SPOOL_DIR so you can
inspect exactly what CUPS sent. Point the CUPS sidecar at it with
    PRINTER_URI=socket://<host>:9100
    PRINTER_MODEL=lsb/usr/cupsfilters/Generic-PDF_Printer-PDF.ppd
"""
import os
import socketserver
import time
from pathlib import Path

SPOOL = Path(os.environ.get("SPOOL_DIR", "/tmp/fake-printer"))
SPOOL.mkdir(parents=True, exist_ok=True)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        data = b"".join(iter(lambda: self.rfile.read(65536), b""))
        if not data:
            return
        ext = "pdf" if data.lstrip()[:5] == b"%PDF-" else "ps" if b"%!PS" in data[:200] else "bin"
        path = SPOOL / f"job-{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns() % 10**6}.{ext}"
        path.write_bytes(data)
        print(f"received {len(data)} bytes from {self.client_address[0]} -> {path}", flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9100"))
    print(f"fake printer listening on :{port}, spooling to {SPOOL}", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()
