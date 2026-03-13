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
ACTIVE_RESOLVERS_FILE="${STORM_ACTIVE_RESOLVERS_FILE:-${BASE_DIR}/state/resolvers_active.txt}"
PICKER_SAMPLE_MODE="${STORM_PICKER_SAMPLE_MODE:-random}"
BOOTSTRAP_ALLOW_FALLBACK="${STORM_BOOTSTRAP_ALLOW_FALLBACK:-0}"

SELECTED=""
if [[ -s "${ACTIVE_RESOLVERS_FILE}" ]]; then
  SELECTED="$(tr '\n' ' ' < "${ACTIVE_RESOLVERS_FILE}" | xargs || true)"
fi

if [[ -z "${SELECTED}" ]]; then
  PICKER_CMD=(
    "${PYTHON_BIN}" "${BASE_DIR}/storm_resolver_picker.py"
    --resolvers-file "${RESOLVER_FILE}"
    --zone "${ZONE}"
    --timeout "${RESOLVER_TIMEOUT}"
    --max-probe "${RESOLVER_MAX_PROBE}"
    --concurrency "${RESOLVER_CONCURRENCY}"
    --sample-mode "${PICKER_SAMPLE_MODE}"
    --take "${RESOLVER_TAKE}"
    --min-healthy "${RESOLVER_MIN_HEALTHY}"
  )
  if [[ "${BOOTSTRAP_ALLOW_FALLBACK}" == "1" ]]; then
    PICKER_CMD+=(--allow-fallback)
  fi
  SELECTED="$("${PICKER_CMD[@]}")"
fi

if [[ -z "${SELECTED}" ]]; then
  echo "No resolvers selected; refusing to start storm_client"
  exit 2
fi

echo "Starting storm_client with resolvers: ${SELECTED}"

exec "${PYTHON_BIN}" "${BASE_DIR}/storm_client.py" \
  --listen "${LISTEN_ADDR}" \
  --zone "${ZONE}" \
  --poll-interval "${POLL_INTERVAL}" \
  --resolvers ${SELECTED}
