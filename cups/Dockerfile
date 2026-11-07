# CUPS server that runs as a sidecar next to the portal and owns the print queue.
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends cups cups-client cups-filters \
 && rm -rf /var/lib/apt/lists/*
COPY cupsd.conf /etc/cups/cupsd.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
 && sed -i -E 's|^#?(ErrorLog\|AccessLog\|PageLog) .*|\1 stderr|' /etc/cups/cups-files.conf \
 && grep -q '^ErrorLog stderr' /etc/cups/cups-files.conf || echo 'ErrorLog stderr' >> /etc/cups/cups-files.conf
EXPOSE 631
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
