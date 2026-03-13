from __future__ import annotations

from pathlib import Path

from storm_stack_doctor import (
    normalize_lf_content,
    patch_base_dir_in_unit,
    patch_service_env_var,
    patch_zone_in_unit,
    render_unit_template,
    services_for_role,
    sync_unit_file,
)


def test_normalize_lf_content_converts_crlf():
    raw = b"#!/usr/bin/env bash\r\necho hi\r\n"
    fixed, changed = normalize_lf_content(raw)
    assert changed is True
    assert fixed == b"#!/usr/bin/env bash\necho hi\n"


def test_normalize_lf_content_keeps_lf():
    raw = b"line1\nline2\n"
    fixed, changed = normalize_lf_content(raw)
    assert changed is False
    assert fixed == raw


def test_patch_zone_in_unit_replaces_zone_arg():
    text = "ExecStart=/x/storm_server.py --listen 0.0.0.0:53 --zone old.example --target 127.0.0.1:10000\n"
    patched, changed = patch_zone_in_unit(text, "new.example")
    assert changed is True
    assert "--zone new.example" in patched
    assert "--zone old.example" not in patched


def test_patch_base_dir_in_unit_replaces_default_path():
    text = "ExecStart=/opt/nexora-storm/storm_server.py --zone z\n"
    patched, changed = patch_base_dir_in_unit(text, "/srv/storm")
    assert changed is True
    assert "/srv/storm/storm_server.py" in patched


def test_services_for_role_mapping():
    assert services_for_role("inside") == ["storm-resolver-scanner", "storm-resolver-daemon", "storm-client"]
    assert services_for_role("outside") == ["storm-server"]


def test_patch_service_env_var_replaces_existing():
    text = "[Service]\nEnvironment=STORM_ZONE=old.example\nExecStart=/bin/true\n"
    patched, changed = patch_service_env_var(text, "STORM_ZONE", "new.example")
    assert changed is True
    assert "Environment=STORM_ZONE=new.example" in patched
    assert "old.example" not in patched


def test_render_unit_template_injects_client_zone_env(tmp_path: Path):
    src = tmp_path / "storm-client.service"
    src.write_text("[Unit]\n[Service]\nExecStart=/opt/nexora-storm/run_storm_client_auto.sh\n", encoding="utf-8")
    rendered = render_unit_template(src, base_dir="/srv/storm", zone="t1.example.ir")
    assert "ExecStart=/srv/storm/run_storm_client_auto.sh" in rendered
    assert "Environment=STORM_ZONE=t1.example.ir" in rendered


def test_sync_unit_file_detects_drift_without_apply(tmp_path: Path):
    src = tmp_path / "storm-server.service"
    dst = tmp_path / "installed.service"
    src.write_text(
        "[Service]\nExecStart=/opt/nexora-storm/storm_server.py --zone t1.phonexpress.ir\n",
        encoding="utf-8",
    )
    dst.write_text(
        "[Service]\nExecStart=/opt/nexora-storm/storm_server.py --zone old.example\n",
        encoding="utf-8",
    )

    result = sync_unit_file(src=src, dst=dst, base_dir="/opt/nexora-storm", zone="new.example", apply=False)
    assert result.ok is False
    assert "drift" in result.detail


def test_sync_unit_file_applies_changes(tmp_path: Path):
    src = tmp_path / "storm-resolver-daemon.service"
    dst = tmp_path / "installed.service"
    src.write_text(
        "[Service]\nExecStart=/opt/nexora-storm/storm_resolver_daemon.py --zone t1.phonexpress.ir\n",
        encoding="utf-8",
    )

    result = sync_unit_file(src=src, dst=dst, base_dir="/srv/storm", zone="z.example", apply=True)
    assert result.ok is True
    data = dst.read_text(encoding="utf-8")
    assert "/srv/storm/storm_resolver_daemon.py" in data
    assert "--zone z.example" in data
