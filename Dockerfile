# syntax=docker/dockerfile:1
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

# cups-client: lp/lpstat/cancel (talks to the CUPS sidecar)
# sane-utils + sane-airscan: scanimage with eSCL/WSD support for network scanners
# fonts-dejavu-core: Unicode font for text annotations
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates cups-client sane-utils sane-airscan fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/venv PATH=/venv/bin:$PATH
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /venv && pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --home-dir /app portal \
 && mkdir -p /data && chown 10001 /data
USER 10001

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    CUPS_SERVER=localhost:631
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
