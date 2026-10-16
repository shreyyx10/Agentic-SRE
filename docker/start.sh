#!/bin/sh
# Gunicorn settings are derived from the same Terraform variables that size the
# Cloud Run service (infra/platform ingest_service / analyse_service), so they
# cannot drift apart:
#   ASRE_THREADS           = Cloud Run max_instance_request_concurrency
#   ASRE_REQUEST_TIMEOUT_S = Cloud Run request timeout
# The worker timeout is deliberately LONGER than the request timeout, so Cloud
# Run's deadline is authoritative and one slow request can never kill a worker
# shared with others (P1-7).
set -eu
: "${PORT:?PORT is set by Cloud Run}"
: "${ASRE_THREADS:?ASRE_THREADS is set by infra/platform}"
: "${ASRE_REQUEST_TIMEOUT_S:?ASRE_REQUEST_TIMEOUT_S is set by infra/platform}"
WORKER_TIMEOUT=$((ASRE_REQUEST_TIMEOUT_S + ${ASRE_TIMEOUT_MARGIN_S:-30}))
GRACEFUL_TIMEOUT=${ASRE_GRACEFUL_TIMEOUT_S:-10}

case "${ASRE_SERVICE:-}" in
  ingest | analyse) ;;
  *)
    echo "ASRE_SERVICE must be 'ingest' or 'analyse'" >&2
    exit 64 ;;
esac

exec gunicorn --bind ":${PORT}" --workers 1 --threads "${ASRE_THREADS}" \
  --timeout "${WORKER_TIMEOUT}" --graceful-timeout "${GRACEFUL_TIMEOUT}" \
  --access-logfile - "asre.${ASRE_SERVICE}.main:app"
