#!/usr/bin/env python3
"""
STORM stack doctor for inside/outside servers.

Checks that required files exist, syncs systemd units from the repository,
normalizes shell script line endings, and validates loaded service state.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys

DEFAULT_BASE_DIR = "/opt/nexora-storm"
ZONE_ARG_RE = re.compile(r"(--zone\s+)(\S+)")
UNIT_NAME_BY_SERVICE = {
    "storm-client": "storm-client.service",
    "storm-resolver-scanner": "storm-resolver-scanner.service",
    "storm-resolver-daemon": "storm-resolver-daemon.service",
    "storm-health-monitor": "storm-health-monitor.service",
    "storm-server": "storm-server.service",
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def normalize_lf_content(raw: bytes) -> tuple[bytes, bool]:
    if b"\r\n" not in raw:
        return raw, False
    return raw.replace(b"\r\n", b"\n"), True


def patch_zone_in_unit(text: str, zone: str) -> tuple[str, bool]:
    if "--zone" not in text:
        return text, False
    patched, count = ZONE_ARG_RE.subn(rf"\1{zone}", text)
    return patched, count > 0


def patch_base_dir_in_unit(text: str, base_dir: str) -> tuple[str, bool]:
    if DEFAULT_BASE_DIR not in text:
        return text, False
    return text.replace(DEFAULT_BASE_DIR, base_dir), True


def patch_service_env_var(text: str, key: str, value: str) -> tuple[str, bool]:
    line = f"Environment={key}={value}"
    pattern = re.compile(rf"^Environment={re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        patched, count = pattern.subn(line, text)
        return patched, count > 0

    marker = "[Service]\n"
    idx = text.find(marker)
    if idx < 0:
        return text, False
    insert_at = idx + len(marker)
    return text[:insert_at] + line + "\n" + text[insert_at:], True


def services_for_role(role: str) -> list[str]:
    if role == "inside":
        return ["storm-resolver-scanner", "storm-resolver-daemon", "storm-client", "storm-health-monitor"]
    if role == "outside":
        return ["storm-server"]
    raise ValueError(f"unsupported role: {role}")


def required_paths(base_dir: Path, role: str) -> list[Path]:
    common = [base_dir / ".venv" / "bin" / "python"]
    if role == "inside":
        return common + [
            base_dir / "storm_client.py",
            base_dir / "storm_resolver_picker.py",
            base_dir / "storm_resolver_daemon.py",
            base_dir / "storm_resolver_scanner.py",
            base_dir / "storm_health_monitor.py",
            base_dir / "run_storm_client_auto.sh",
            base_dir / "data" / "resolvers.txt",
            base_dir / "systemd" / "storm-client.service",
            base_dir / "systemd" / "storm-resolver-scanner.service",
            base_dir / "systemd" / "storm-resolver-daemon.service",
            base_dir / "systemd" / "storm-health-monitor.service",
        ]
    if role == "outside":
        return common + [
            base_dir / "storm_server.py",
            base_dir / "systemd" / "storm-server.service",
        ]
    raise ValueError(f"unsupported role: {role}")


def render_unit_template(src: Path, base_dir: str, zone: str) -> str:
    text = src.read_text(encoding="utf-8")
    text, _ = patch_base_dir_in_unit(text, base_dir)
    text, _ = patch_zone_in_unit(text, zone)
    if src.name == "storm-client.service":
        text, _ = patch_service_env_var(text, "STORM_ZONE", zone)
    return text


def sync_unit_file(
    src: Path,
    dst: Path,
    base_dir: str,
    zone: str,
    apply: bool,
) -> CheckResult:
    expected = render_unit_template(src, base_dir=base_dir, zone=zone)

    current = ""
    if dst.exists():
        current = dst.read_text(encoding="utf-8")

    if current == expected:
        return CheckResult(name=f"unit {dst.name}", ok=True, detail="up-to-date")

    if not apply:
        return CheckResult(
            name=f"unit {dst.name}",
            ok=False,
            detail="drift detected; rerun with --apply",
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(expected, encoding="utf-8", newline="\n")
    return CheckResult(name=f"unit {dst.name}", ok=True, detail="synced")


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def ensure_lf_script(path: Path, apply: bool) -> CheckResult:
    raw = path.read_bytes()
    normalized, changed = normalize_lf_content(raw)
    if not changed:
        return CheckResult(name=f"line endings {path.name}", ok=True, detail="LF")

    if not apply:
        return CheckResult(
            name=f"line endings {path.name}",
            ok=False,
            detail="CRLF found; rerun with --apply",
        )

    path.write_bytes(normalized)
    os.chmod(path, 0o755)
    return CheckResult(name=f"line endings {path.name}", ok=True, detail="fixed to LF")


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def expected_exec_tokens(service: str, zone: str, base_dir: str) -> list[str]:
    if service == "storm-client":
        return [f"{base_dir}/run_storm_client_auto.sh"]
    if service == "storm-resolver-scanner":
        return [
            f"{base_dir}/storm_resolver_scanner.py",
            f"--zone {zone}",
            "--output",
            "--max-probe 300",
        ]
    if service == "storm-resolver-daemon":
        return [
            f"{base_dir}/storm_resolver_daemon.py",
            f"--zone {zone}",
            "--probe-mode random",
            "--max-probe 300",
            "--scanner-json",
            "--healthy-out",
        ]
    if service == "storm-server":
        return [
            f"{base_dir}/storm_server.py",
            f"--zone {zone}",
            "--listen 0.0.0.0:53",
        ]
    if service == "storm-health-monitor":
        return [
            f"{base_dir}/storm_health_monitor.py",
            "--proxy-port 1443",
            "--loop 30",
        ]
    return []


def expected_environment_tokens(service: str, zone: str) -> list[str]:
    if service == "storm-client":
        return [f"STORM_ZONE={zone}"]
    return []


def verify_loaded_service(service: str, zone: str, base_dir: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    rc_active, out_active, err_active = run_cmd(["systemctl", "is-active", service])
    active_ok = rc_active == 0 and out_active == "active"
    results.append(
        CheckResult(
            name=f"service {service} active",
            ok=active_ok,
            detail=out_active or err_active,
        )
    )

    rc_exec, out_exec, err_exec = run_cmd(["systemctl", "show", "-p", "ExecStart", "--value", service])
    if rc_exec != 0:
        results.append(
            CheckResult(
                name=f"service {service} execstart",
                ok=False,
                detail=err_exec or "cannot read ExecStart",
            )
        )
        return results

    missing = [token for token in expected_exec_tokens(service, zone, base_dir) if token not in out_exec]
    results.append(
        CheckResult(
            name=f"service {service} execstart",
            ok=not missing,
            detail=out_exec if not missing else f"missing tokens: {', '.join(missing)}",
        )
    )

    env_tokens = expected_environment_tokens(service, zone)
    if env_tokens:
        rc_env, out_env, err_env = run_cmd(["systemctl", "show", "-p", "Environment", "--value", service])
        if rc_env != 0:
            results.append(
                CheckResult(
                    name=f"service {service} environment",
                    ok=False,
                    detail=err_env or "cannot read Environment",
                )
            )
        else:
            missing_env = [token for token in env_tokens if token not in out_env]
            results.append(
                CheckResult(
                    name=f"service {service} environment",
                    ok=not missing_env,
                    detail=out_env if not missing_env else f"missing tokens: {', '.join(missing_env)}",
                )
            )
    return results


def require_root_if_apply(apply: bool) -> CheckResult | None:
    if not apply:
        return None

    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() != 0:
        return CheckResult(
            name="permissions",
            ok=False,
            detail="run with sudo for --apply",
        )
    return None


def print_results(results: list[CheckResult]) -> None:
    for item in results:
        status = "PASS" if item.ok else "FAIL"
        suffix = f" :: {item.detail}" if item.detail else ""
        print(f"[{status}] {item.name}{suffix}")

    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    print(f"\nSummary: passed={passed} failed={failed}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STORM stack doctor")
    parser.add_argument("--role", choices=["inside", "outside"], required=True)
    parser.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    parser.add_argument("--zone", default="t1.phonexpress.ir")
    parser.add_argument("--apply", action="store_true", help="Apply fixes and restart services")
    parser.add_argument("--no-restart", action="store_true", help="Do not restart services in --apply mode")
    parser.add_argument("--strict", action="store_true", help="Fail if service is not active in dry-run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = Path(args.base_dir)
    results: list[CheckResult] = []

    perm = require_root_if_apply(args.apply)
    if perm is not None:
        results.append(perm)
        print_results(results)
        return 2

    req = required_paths(base_dir, args.role)
    missing = [str(p) for p in req if not p.exists()]
    if missing:
        results.append(CheckResult(name="required files", ok=False, detail="; ".join(missing)))
    else:
        results.append(CheckResult(name="required files", ok=True, detail="ok"))

    if args.role == "inside":
        script_path = base_dir / "run_storm_client_auto.sh"
        if script_path.exists():
            results.append(ensure_lf_script(script_path, apply=args.apply))

    # Sync unit files from repository templates.
    unit_dir = base_dir / "systemd"
    for service in services_for_role(args.role):
        unit_name = UNIT_NAME_BY_SERVICE[service]
        src = unit_dir / unit_name
        dst = Path("/etc/systemd/system") / unit_name
        if src.exists():
            results.append(
                sync_unit_file(
                    src=src,
                    dst=dst,
                    base_dir=str(base_dir),
                    zone=args.zone,
                    apply=args.apply,
                )
            )
        else:
            results.append(CheckResult(name=f"unit template {unit_name}", ok=False, detail="not found in repo"))

    if not systemctl_available():
        results.append(CheckResult(name="systemctl", ok=False, detail="systemctl command not found"))
        print_results(results)
        return 2

    if args.apply and all(r.ok for r in results):
        rc, out, err = run_cmd(["systemctl", "daemon-reload"])
        results.append(CheckResult(name="systemctl daemon-reload", ok=rc == 0, detail=out or err))

        for service in services_for_role(args.role):
            rc_enable, out_enable, err_enable = run_cmd(["systemctl", "enable", service])
            results.append(
                CheckResult(
                    name=f"systemctl enable {service}",
                    ok=rc_enable == 0,
                    detail=out_enable or err_enable,
                )
            )
            if not args.no_restart:
                rc_restart, out_restart, err_restart = run_cmd(["systemctl", "restart", service])
                results.append(
                    CheckResult(
                        name=f"systemctl restart {service}",
                        ok=rc_restart == 0,
                        detail=out_restart or err_restart,
                    )
                )
            else:
                rc_start, out_start, err_start = run_cmd(["systemctl", "start", service])
                results.append(
                    CheckResult(
                        name=f"systemctl start {service}",
                        ok=rc_start == 0,
                        detail=out_start or err_start,
                    )
                )

    for service in services_for_role(args.role):
        results.extend(verify_loaded_service(service=service, zone=args.zone, base_dir=str(base_dir)))

    print_results(results)

    failures = [r for r in results if not r.ok]
    if failures:
        if args.strict:
            return 2
        # In dry-run mode, active-state failures are expected on fresh host.
        hard_fail = [
            r
            for r in failures
            if not r.name.startswith("service ") or args.apply
        ]
        return 2 if hard_fail else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
