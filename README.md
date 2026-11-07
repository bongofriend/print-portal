# Print Portal

A self-hosted web portal for an office printer / MFP:

- **Print**: upload PDFs or images and print them with copies, duplex, colour, paper size, page ranges and orientation. Track the queue and cancel jobs.
- **Scan**: scan from the flatbed or document feeder to PDF, PNG or JPEG, then download it or move it into Documents.
- **Printer status**: online/offline, idle/printing/stopped, state reasons (paper jam, toner low…), supply levels, queue length, and scanner availability.
- **PDF annotation**: in-browser pen, highlighter, box, text and white-out, with undo/redo and zoom. Annotations are saved separately from the original and burned into the PDF when you download or print.
- **Containerised and deployed to Kubernetes**: a portal container plus a CUPS sidecar in one pod, packaged with Kustomize.

```
                     ┌───────────────────────── Pod ─────────────────────────┐
 Browser ── Ingress ─┤  portal (FastAPI + static UI)        cups (cupsd)     │
                     │   :8080                          ◄──  localhost:631    │── IPP / socket ──► Printer
                     │   scanimage (SANE + airscan) ─────────────────────────│── eSCL / saned ──► Scanner
                     │   /data  (PVC: documents, scans, job log)             │
                     └───────────────────────────────────────────────────────┘
```

The UI is plain HTML, CSS and JS with no CDN or build step, so it works on air-gapped clusters.

## Quick start (Docker Compose)

```bash
docker compose up --build
```

Open http://localhost:8080. The stack mirrors the Kubernetes pod (portal and CUPS share a network namespace). A fake raw-socket printer (`dev/fake_printer.py`) stores every job it receives, so you can check exactly what CUPS sent:

```bash
docker compose exec fake-printer ls -l /tmp/spool
```

The scanner is simulated (`SCANNER_BACKEND=mock`).

### Without Docker

```bash
pip install -r requirements-dev.txt
PRINTER_BACKEND=mock SCANNER_BACKEND=mock uvicorn --factory app.main:create_app --reload --port 8080
pytest
```

## Deploying to Kubernetes

1. **Build and push the images.** Both images default to `ubuntu:24.04`; override with `--build-arg BASE_IMAGE=...` to use a mirror.

   ```bash
   docker build -t ghcr.io/your-org/print-portal:1.0.0 .
   ```

   ```bash
   docker build -t ghcr.io/your-org/print-portal-cups:1.0.0 cups
   ```

   ```bash
   docker push ghcr.io/your-org/print-portal:1.0.0 && docker push ghcr.io/your-org/print-portal-cups:1.0.0
   ```

2. **Configure** `k8s/kustomization.yaml` (image names) and `k8s/configmap.yaml`:

   | Setting | Meaning |
   |---|---|
   | `PRINTER_URI` | The printer's address. IPP Everywhere / AirPrint printers: `ipp://<ip>/ipp/print`. Older printers: `socket://<ip>:9100` |
   | `PRINTER_MODEL` | `everywhere` (driverless, recommended) or a CUPS model from `lpinfo -m`, e.g. `drv:///sample.drv/generic.ppd` |
   | `PRINTER_OPTIONS` | Queue defaults, e.g. `media=iso_a4_210x297mm sides=two-sided-long-edge` |
   | `airscan.conf` | The scanner's eSCL URL, usually `http://<ip>/eSCL`. Network-scanner discovery uses mDNS, which doesn't cross the pod network, so list the scanner explicitly |
   | `SCANNER_DEVICE` | Optional SANE device name, e.g. `airscan:e0:Office MFP`. Empty means the first device found |
   | `RETENTION_DAYS` | Documents and scans older than this are deleted (`0` keeps them forever) |
   | `MAX_UPLOAD_MB` | Upload limit. Keep the ingress `proxy-body-size` in sync |

   Set the host in `k8s/ingress.yaml`. Then deploy:

   ```bash
   kubectl apply -k k8s/
   ```

3. **Authentication.** The portal can print and scan for anyone who can reach it. Either put it behind your SSO / auth proxy at the ingress, or turn on the built-in HTTP basic auth:

   ```bash
   kubectl -n print-portal create secret generic print-portal-auth --from-literal=username=admin --from-literal=password='change-me'
   ```

   ```bash
   kubectl -n print-portal rollout restart deploy/print-portal
   ```

### Design notes

- **One replica, `Recreate` strategy.** A print queue and a scanner are single physical devices, and the data volume is `ReadWriteOnce`.
- **CUPS sidecar.** The sidecar creates the queue from environment variables at every start, so it holds no state worth keeping. `cupsd` listens only on `localhost` inside the pod and is never exposed. The `everywhere` model asks the printer for its capabilities, so the sidecar keeps retrying (and its startup probe stays pending) until the printer answers. A printer that is switched off just delays readiness.
- **Hardening.** The portal runs as non-root (uid 10001) with a read-only root filesystem and all capabilities dropped. The CUPS sidecar starts as root (cupsd needs it to drop to the `lp` user) with only `CHOWN SETUID SETGID DAC_OVERRIDE FOWNER KILL`. This passes the `baseline` Pod Security Standard.
- **Annotations are flattened** into the page content rather than stored as PDF annotation objects, so every viewer and printer driver renders them the same way. Rotated pages are handled. The original upload is never modified.
- **White-out is cosmetic.** It covers content visually, but the text underneath is still in the PDF. It is not a secure redaction.

## Configuration reference (portal)

| Variable | Default | |
|---|---|---|
| `DATA_DIR` | `./data` (`/data` in the image) | Documents, scans, job log |
| `PRINTER_BACKEND` | `cups` | `cups` or `mock` |
| `PRINTER_NAME` | `office` | CUPS queue name |
| `CUPS_SERVER` | `localhost:631` in the image | Standard CUPS client variable |
| `SCANNER_BACKEND` | `sane` | `sane` or `mock` |
| `SCANNER_DEVICE` | *(first found)* | SANE device name |
| `SCAN_TIMEOUT_SECONDS` | `300` | |
| `MAX_UPLOAD_MB` | `50` | |
| `RETENTION_DAYS` | `30` | `0` disables cleanup |
| `PORTAL_USER` / `PORTAL_PASSWORD` | *(unset)* | Enables HTTP basic auth when both are set |

## API

Interactive docs are served at `/api/docs`.

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Printer and scanner status |
| GET / POST | `/api/documents` | List / upload (multipart `file`) |
| GET / PATCH / DELETE | `/api/documents/{id}` | Metadata and page sizes / rename / delete |
| GET | `/api/documents/{id}/download?annotated=true` | PDF, with annotations burned in by default |
| GET | `/api/documents/{id}/pages/{n}?width=1000` | Page rendered as PNG |
| GET / PUT | `/api/documents/{id}/annotations` | Annotation JSON |
| POST | `/api/print` | `{document_id, include_annotations, options:{copies, sides, color, media, page_ranges, orientation, fit_to_page}}` |
| GET / DELETE | `/api/jobs`, `/api/jobs/{id}` | Queue / cancel |
| GET / POST | `/api/scans` | List / start a scan `{source, mode, resolution, format}` |
| GET | `/api/scans/{id}/download` | Scan result |
| POST | `/api/scans/{id}/to-document` | Copy a scan into Documents |

## Limitations

- Uploads must be PDFs or images (PNG, JPEG, TIFF, GIF, BMP, WebP). Office documents would need LibreOffice in the image for conversion.
- Supply levels appear only if the printer reports them. With CUPS they update after the printer has processed a job.
- For a manually configured eSCL scanner, SANE lists the device without probing it, so the status page shows "Ready" even when it's switched off. The scan itself then fails with a clear error.
- USB-only printers and scanners would need the device passed into the pod (`hostPath` plus node affinity). Network devices are the intended setup.

## Project layout

```
app/            FastAPI backend (main.py, printer.py, scanner.py, documents.py, annotate.py) + static/ UI
cups/           CUPS sidecar image (Dockerfile, cupsd.conf, entrypoint.sh)
k8s/            Kustomize manifests
dev/            fake_printer.py for local testing
tests/          pytest suite (API, annotation rendering, CUPS output parsing)
```
