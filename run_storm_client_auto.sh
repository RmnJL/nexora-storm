#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/opt/nexora-storm"
PYTHON_BIN="${BASE_DIR}/.venv/bin/python"
ZONE="${STORM_ZONE:-t1.phonexpress.ir}"
LISTEN_ADDR="${STORM_LISTEN:-127.0.0.1:1443}"
POLL_INTERVAL="${STORM_POLL_INTERVAL:-0.2}"
DNS_TIMEOUT="${STORM_DNS_TIMEOUT:-3.0}"
RESOLVER_FANOUT="${STORM_RESOLVER_FANOUT:-3}"
RESOLVER_FILE="${STORM_RESOLVER_FILE:-${BASE_DIR}/data/resolvers.txt}"
RESOLVER_TAKE="${STORM_RESOLVER_TAKE:-8}"
RESOLVER_MIN_SELECTED="${STORM_RESOLVER_MIN_SELECTED:-4}"
RESOLVER_MAX_SELECTED="${STORM_RESOLVER_MAX_SELECTED:-8}"
RESOLVER_MIN_HEALTHY="${STORM_RESOLVER_MIN_HEALTHY:-2}"
RESOLVER_TIMEOUT="${STORM_RESOLVER_TIMEOUT:-1.5}"
RESOLVER_MAX_PROBE="${STORM_RESOLVER_MAX_PROBE:-40}"
RESOLVER_CONCURRENCY="${STORM_RESOLVER_CONCURRENCY:-15}"
ACTIVE_RESOLVERS_FILE="${STORM_ACTIVE_RESOLVERS_FILE:-${BASE_DIR}/state/resolvers_active.txt}"
HEALTHY_RESOLVERS_FILE="${STORM_HEALTHY_RESOLVERS_FILE:-${BASE_DIR}/state/resolvers_healthy.txt}"
PICKER_SAMPLE_MODE="${STORM_PICKER_SAMPLE_MODE:-random}"
BOOTSTRAP_ALLOW_FALLBACK="${STORM_BOOTSTRAP_ALLOW_FALLBACK:-0}"

normalize_tokens() {
  local raw="$1"
  local max_items="$2"
  echo "${raw}" \
    | tr ' ' '\n' \
    | sed '/^$/d' \
    | awk '!seen[$0]++' \
    | head -n "${max_items}" \
    | xargs || true
}

token_count() {
  local raw="$1"
  if [[ -z "${raw}" ]]; then
    echo 0
    return
  fi
  echo "${raw}" | awk '{print NF}'
}

merge_tokens() {
  local first="$1"
  local second="$2"
  local max_items="$3"
  printf '%s\n%s\n' "${first}" "${second}" \
    | tr ' ' '\n' \
    | sed '/^$/d' \
    | awk '!seen[$0]++' \
    | head -n "${max_items}" \
    | xargs || true
}

SELECTED=""
if [[ -s "${ACTIVE_RESOLVERS_FILE}" ]]; then
  SELECTED_RAW="$(tr '\n' ' ' < "${ACTIVE_RESOLVERS_FILE}" | xargs || true)"
  SELECTED="$(normalize_tokens "${SELECTED_RAW}" "${RESOLVER_MAX_SELECTED}")"
fi

if [[ "$(token_count "${SELECTED}")" -lt "${RESOLVER_MIN_SELECTED}" ]] && [[ -s "${HEALTHY_RESOLVERS_FILE}" ]]; then
  HEALTHY_RAW="$(tr '\n' ' ' < "${HEALTHY_RESOLVERS_FILE}" | xargs || true)"
  HEALTHY_TOP="$(normalize_tokens "${HEALTHY_RAW}" "${RESOLVER_MAX_SELECTED}")"
  SELECTED="$(merge_tokens "${SELECTED}" "${HEALTHY_TOP}" "${RESOLVER_MAX_SELECTED}")"
fi

if [[ "$(token_count "${SELECTED}")" -lt "${RESOLVER_MIN_SELECTED}" ]]; then
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
  PICKED="$("${PICKER_CMD[@]}")"
  PICKED_TOP="$(normalize_tokens "${PICKED}" "${RESOLVER_MAX_SELECTED}")"
  SELECTED="$(merge_tokens "${SELECTED}" "${PICKED_TOP}" "${RESOLVER_MAX_SELECTED}")"
fi

if [[ "$(token_count "${SELECTED}")" -lt 1 ]]; then
  echo "No resolvers selected; refusing to start storm_client"
  exit 2
fi

echo "Starting storm_client with resolvers: ${SELECTED}"

exec "${PYTHON_BIN}" "${BASE_DIR}/storm_client.py" \
  --listen "${LISTEN_ADDR}" \
  --zone "${ZONE}" \
  --poll-interval "${POLL_INTERVAL}" \
  --dns-timeout "${DNS_TIMEOUT}" \
  --resolver-fanout "${RESOLVER_FANOUT}" \
  --resolvers ${SELECTED}
