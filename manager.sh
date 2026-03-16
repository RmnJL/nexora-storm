#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR_DEFAULT="/opt/nexora-storm"
ZONE_DEFAULT="t1.phonexpress.ir"
MONITOR_TARGET_DEFAULT="65.109.221.2:8443"

INSIDE_SERVICES=(storm-resolver-scanner storm-resolver-daemon storm-client storm-health-monitor)
OUTSIDE_SERVICES=(storm-server)

usage() {
  cat <<'EOF'
STORM manager.sh

Usage:
  manager.sh deploy-inside [--archive /tmp/nexora-storm-<sha>.tgz] [--install-dir /opt/nexora-storm] [--zone t1.mehrmarja.ir] [--monitor-target 65.109.221.2:8443]
  manager.sh deploy-outside [--archive /tmp/nexora-storm-<sha>.tgz] [--install-dir /opt/nexora-storm] [--zone t1.mehrmarja.ir]
  manager.sh update-inside [same args as deploy-inside]
  manager.sh update-outside [same args as deploy-outside]
  manager.sh status-inside
  manager.sh status-outside
  manager.sh restart-inside
  manager.sh restart-outside
  manager.sh stop-inside
  manager.sh stop-outside
  manager.sh start-inside
  manager.sh start-outside
  manager.sh logs --service storm-client [--lines 120] [--follow]
  manager.sh health-now [--install-dir /opt/nexora-storm] [--target-host 65.109.221.2] [--target-port 8443]
  manager.sh acceptance-a7 [--install-dir /opt/nexora-storm] [--duration-sec 3600] [--interval-sec 30]
  manager.sh doctor-inside [--install-dir /opt/nexora-storm] [--zone t1.mehrmarja.ir] [--apply]
  manager.sh doctor-outside [--install-dir /opt/nexora-storm] [--zone t1.mehrmarja.ir] [--apply]

Notes:
  - deploy/update commands need root.
  - If --archive is omitted, current directory source is used.
EOF
}

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: run as root (sudo -i)" >&2
    exit 1
  fi
}

find_project_root() {
  local base="$1"
  local found
  found="$(find "$base" -maxdepth 5 -type f -name storm_client.py 2>/dev/null | head -n1 || true)"
  if [[ -z "$found" ]]; then
    return 1
  fi
  dirname "$found"
}

copy_tree() {
  local src="$1"
  local dst="$2"
  local src_real dst_real
  src_real="$(cd "$src" && pwd)"
  dst_real="$(cd "$(dirname "$dst")" && pwd)/$(basename "$dst")"
  if [[ "$src_real" == "$dst_real" ]]; then
    return 0
  fi
  rm -rf "$dst"
  mkdir -p "$dst"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$src"/ "$dst"/
  else
    cp -a "$src"/. "$dst"/
  fi
}

install_venv_requirements() {
  local base="$1"
  local req="$base/requirements.txt"
  if [[ ! -f "$req" ]]; then
    echo "ERROR: requirements.txt not found at $req" >&2
    exit 1
  fi
  python3 -m venv "$base/.venv"
  "$base/.venv/bin/pip" install --upgrade pip
  if ! "$base/.venv/bin/pip" install \
    --trusted-host mirror-pypi.runflare.com \
    -i https://mirror-pypi.runflare.com/simple \
    -r "$req" \
    --timeout 120 \
    --retries 20; then
    "$base/.venv/bin/pip" install \
      -i https://pypi.org/simple \
      --trusted-host pypi.org \
      --trusted-host files.pythonhosted.org \
      -r "$req" \
      --timeout 120 \
      --retries 20
  fi
}

normalize_scripts() {
  local base="$1"
  if [[ -f "$base/run_storm_client_auto.sh" ]]; then
    sed -i 's/\r$//' "$base/run_storm_client_auto.sh"
    chmod +x "$base/run_storm_client_auto.sh"
  fi
}

run_doctor() {
  local role="$1"
  local base="$2"
  local zone="$3"
  local apply="$4"
  local cmd=( "$base/.venv/bin/python" "$base/storm_stack_doctor.py" --role "$role" --base-dir "$base" --zone "$zone" )
  if [[ "$apply" == "1" ]]; then
    cmd+=( --apply )
  fi
  "${cmd[@]}"
}

ensure_monitor_targets() {
  local base="$1"
  local target="$2"
  mkdir -p "$base/state"
  echo "$target" > "$base/state/monitor_targets.txt"
}

deploy_role() {
  local role="$1"
  shift

  local archive=""
  local install_dir="$INSTALL_DIR_DEFAULT"
  local zone="$ZONE_DEFAULT"
  local monitor_target="$MONITOR_TARGET_DEFAULT"
  local apply_doctor=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --archive) archive="$2"; shift 2 ;;
      --install-dir) install_dir="$2"; shift 2 ;;
      --zone) zone="$2"; shift 2 ;;
      --monitor-target) monitor_target="$2"; shift 2 ;;
      --no-apply) apply_doctor=0; shift ;;
      *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
  done

  need_root
  local src_dir=""
  local tmp_dir=""

  if [[ -n "$archive" ]]; then
    if [[ ! -f "$archive" ]]; then
      echo "ERROR: archive not found: $archive" >&2
      exit 1
    fi
    tmp_dir="$(mktemp -d)"
    tar -xzf "$archive" -C "$tmp_dir"
    if ! src_dir="$(find_project_root "$tmp_dir")"; then
      echo "ERROR: could not locate project root in archive" >&2
      rm -rf "$tmp_dir"
      exit 1
    fi
  else
    if ! src_dir="$(find_project_root "$SCRIPT_DIR")"; then
      echo "ERROR: could not locate project root from script dir: $SCRIPT_DIR" >&2
      exit 1
    fi
  fi

  if [[ "$role" == "inside" ]]; then
    systemctl stop "${INSIDE_SERVICES[@]}" || true
  else
    systemctl stop "${OUTSIDE_SERVICES[@]}" || true
  fi

  copy_tree "$src_dir" "$install_dir"
  install_venv_requirements "$install_dir"
  normalize_scripts "$install_dir"
  mkdir -p "$install_dir/state"

  if [[ "$role" == "inside" ]]; then
    ensure_monitor_targets "$install_dir" "$monitor_target"
  fi

  run_doctor "$role" "$install_dir" "$zone" "$apply_doctor"

  if [[ -n "$tmp_dir" ]]; then
    rm -rf "$tmp_dir"
  fi

  echo "OK: deploy-$role completed (install_dir=$install_dir zone=$zone)"
}

service_group_action() {
  local role="$1"
  local action="$2"
  need_root
  if [[ "$role" == "inside" ]]; then
    systemctl "$action" "${INSIDE_SERVICES[@]}"
  else
    systemctl "$action" "${OUTSIDE_SERVICES[@]}"
  fi
}

service_group_status() {
  local role="$1"
  if [[ "$role" == "inside" ]]; then
    systemctl status "${INSIDE_SERVICES[@]}" --no-pager -l
  else
    systemctl status "${OUTSIDE_SERVICES[@]}" --no-pager -l
  fi
}

logs_cmd() {
  local service=""
  local lines=120
  local follow=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service) service="$2"; shift 2 ;;
      --lines) lines="$2"; shift 2 ;;
      --follow) follow=1; shift ;;
      *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
  done
  if [[ -z "$service" ]]; then
    echo "ERROR: --service is required" >&2
    exit 1
  fi
  if [[ "$follow" == "1" ]]; then
    journalctl -u "$service" -n "$lines" -f -l
  else
    journalctl -u "$service" -n "$lines" --no-pager -l
  fi
}

health_now_cmd() {
  local install_dir="$INSTALL_DIR_DEFAULT"
  local target_host="65.109.221.2"
  local target_port=8443
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) install_dir="$2"; shift 2 ;;
      --target-host) target_host="$2"; shift 2 ;;
      --target-port) target_port="$2"; shift 2 ;;
      *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
  done
  "$install_dir/.venv/bin/python" "$install_dir/storm_health_check.py" \
    --proxy-host 127.0.0.1 \
    --proxy-port 1443 \
    --target-host "$target_host" \
    --target-port "$target_port" \
    --checks 20 \
    --interval 0.5 \
    --timeout 10 \
    --success-threshold 0.20 \
    --latency-threshold-ms 5000 \
    --json-out "$install_dir/state/health_now.json"
  jq '.stats' "$install_dir/state/health_now.json"
}

acceptance_a7_cmd() {
  local install_dir="$INSTALL_DIR_DEFAULT"
  local duration=3600
  local interval=30
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) install_dir="$2"; shift 2 ;;
      --duration-sec) duration="$2"; shift 2 ;;
      --interval-sec) interval="$2"; shift 2 ;;
      *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
  done
  "$install_dir/.venv/bin/python" "$install_dir/storm_acceptance_a7.py" \
    --active-file "$install_dir/state/resolvers_active.txt" \
    --scanner-json "$install_dir/state/resolvers_scan.json" \
    --health-report-json "$install_dir/state/health_monitor_report.json" \
    --duration-sec "$duration" \
    --sample-interval-sec "$interval" \
    --min-success-rate 0.15 \
    --min-success-ratio 0.60 \
    --max-flaps-per-hour 12 \
    --require-reentry-count 1 \
    --json-out "$install_dir/state/a7_acceptance_report.json"
  jq '.result' "$install_dir/state/a7_acceptance_report.json"
}

doctor_cmd() {
  local role="$1"
  shift
  local install_dir="$INSTALL_DIR_DEFAULT"
  local zone="$ZONE_DEFAULT"
  local apply=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) install_dir="$2"; shift 2 ;;
      --zone) zone="$2"; shift 2 ;;
      --apply) apply=1; shift ;;
      *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
  done
  if [[ "$apply" == "1" ]]; then
    need_root
  fi
  run_doctor "$role" "$install_dir" "$zone" "$apply"
}

main() {
  local cmd="${1:-}"
  if [[ -z "$cmd" ]]; then
    usage
    exit 1
  fi
  shift || true

  case "$cmd" in
    deploy-inside|update-inside) deploy_role inside "$@" ;;
    deploy-outside|update-outside) deploy_role outside "$@" ;;
    start-inside) service_group_action inside start ;;
    stop-inside) service_group_action inside stop ;;
    restart-inside) service_group_action inside restart ;;
    status-inside) service_group_status inside ;;
    start-outside) service_group_action outside start ;;
    stop-outside) service_group_action outside stop ;;
    restart-outside) service_group_action outside restart ;;
    status-outside) service_group_status outside ;;
    logs) logs_cmd "$@" ;;
    health-now) health_now_cmd "$@" ;;
    acceptance-a7) acceptance_a7_cmd "$@" ;;
    doctor-inside) doctor_cmd inside "$@" ;;
    doctor-outside) doctor_cmd outside "$@" ;;
    help|-h|--help) usage ;;
    *) echo "ERROR: unknown command: $cmd" >&2; usage; exit 1 ;;
  esac
}

main "$@"
