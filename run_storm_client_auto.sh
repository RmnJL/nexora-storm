#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/opt/nexora-storm"
PYTHON_BIN="${BASE_DIR}/.venv/bin/python"
ZONE="${STORM_ZONE:-t1.phonexpress.ir}"
LISTEN_ADDR="${STORM_LISTEN:-127.0.0.1:1443}"
POLL_INTERVAL="${STORM_POLL_INTERVAL:-0.2}"
RESOLVER_FILE="${STORM_RESOLVER_FILE:-${BASE_DIR}/data/resolvers.txt}"
RESOLVER_TAKE="${STORM_RESOLVER_TAKE:-4}"
RESOLVER_MIN_HEALTHY="${STORM_RESOLVER_MIN_HEALTHY:-2}"
RESOLVER_TIMEOUT="${STORM_RESOLVER_TIMEOUT:-1.5}"
RESOLVER_MAX_PROBE="${STORM_RESOLVER_MAX_PROBE:-40}"
RESOLVER_CONCURRENCY="${STORM_RESOLVER_CONCURRENCY:-15}"

SELECTED="$(
  "${PYTHON_BIN}" "${BASE_DIR}/storm_resolver_picker.py" \
    --resolvers-file "${RESOLVER_FILE}" \
    --zone "${ZONE}" \
    --timeout "${RESOLVER_TIMEOUT}" \
    --max-probe "${RESOLVER_MAX_PROBE}" \
    --concurrency "${RESOLVER_CONCURRENCY}" \
    --take "${RESOLVER_TAKE}" \
    --min-healthy "${RESOLVER_MIN_HEALTHY}" \
    --allow-fallback
)"

if [[ -z "${SELECTED}" ]]; then
  echo "No resolvers selected; refusing to start storm_client"
  exit 2
fi

exec "${PYTHON_BIN}" "${BASE_DIR}/storm_client.py" \
  --listen "${LISTEN_ADDR}" \
  --zone "${ZONE}" \
  --poll-interval "${POLL_INTERVAL}" \
  --resolvers ${SELECTED}

