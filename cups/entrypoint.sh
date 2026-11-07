#!/bin/sh
# Start cupsd and (re)create the print queue from environment variables.
#   PRINTER_URI       required, e.g. ipp://192.168.1.50/ipp/print or ipps://printer.local/ipp/print
#   PRINTER_NAME      queue name (default: office) - must match the portal's PRINTER_NAME
#   PRINTER_MODEL     lpadmin -m model (default: everywhere = driverless IPP Everywhere / AirPrint)
#   PRINTER_PPD       path to a PPD file (e.g. mounted from a ConfigMap); overrides PRINTER_MODEL
#   PRINTER_LOCATION  free text shown in the portal
#   PRINTER_OPTIONS   extra "-o name=value" defaults, space separated, e.g. "media=A4 sides=two-sided-long-edge"
set -eu
PRINTER_NAME="${PRINTER_NAME:-office}"
mkdir -p /run/cups

: "${PRINTER_URI:?PRINTER_URI must be set, e.g. ipp://printer.example.com/ipp/print}"

/usr/sbin/cupsd -f &
CUPSD=$!
trap 'kill -TERM "$CUPSD" 2>/dev/null; wait "$CUPSD"; exit 0' TERM INT

i=0
until lpstat -r >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -gt 30 ] && { echo "cupsd did not start" >&2; exit 1; }
  sleep 1
done

configure() {
  if [ -n "${PRINTER_PPD:-}" ]; then
    lpadmin -p "$PRINTER_NAME" -E -v "$PRINTER_URI" -P "$PRINTER_PPD"
  else
    # "everywhere" queries the printer for its capabilities, so it must be reachable.
    lpadmin -p "$PRINTER_NAME" -E -v "$PRINTER_URI" -m "${PRINTER_MODEL:-everywhere}"
  fi
}
until configure; do
  echo "Could not set up queue '$PRINTER_NAME' for $PRINTER_URI (printer unreachable?), retrying in 15s" >&2
  sleep 15
done
lpadmin -d "$PRINTER_NAME"
lpadmin -p "$PRINTER_NAME" -o printer-error-policy=retry-job
[ -n "${PRINTER_LOCATION:-}" ] && lpadmin -p "$PRINTER_NAME" -L "$PRINTER_LOCATION"
for opt in ${PRINTER_OPTIONS:-}; do lpadmin -p "$PRINTER_NAME" -o "$opt"; done
cupsenable "$PRINTER_NAME"
cupsaccept "$PRINTER_NAME"
echo "Queue '$PRINTER_NAME' ready -> $PRINTER_URI"
touch /run/cups/ready

wait "$CUPSD"
