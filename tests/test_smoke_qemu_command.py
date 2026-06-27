"""Tests for the QEMU command-line builder used by the image smoke test."""

from pathlib import Path

from tests.smoke.qemu_command import (
    OVMF_CODE,
    QemuRunSpec,
    build_kernel_cmdline,
    build_qemu_command,
)


def _spec(tmp_path: Path) -> QemuRunSpec:
    return QemuRunSpec(
        qemu_binary="/usr/bin/qemu-system-x86_64",
        kernel=tmp_path / "vmlinuz",
        initrd=tmp_path / "initrd.img",
        fetch_url="http://10.0.2.2:8000/fleetboot.squashfs",
        fleetboot_server_url="http://10.0.2.2:8000/",
        boot_token="deadbeef",
        host_port=8000,
        vars_file=tmp_path / "OVMF_VARS.fd",
        serial_log=tmp_path / "serial.log",
    )


def test_cmdline_includes_reporter_settings(tmp_path):
    cmdline = build_kernel_cmdline(_spec(tmp_path))
    assert "fleetboot.server=http://10.0.2.2:8000/" in cmdline
    assert "fleetboot.boot_token=deadbeef" in cmdline


def test_cmdline_uses_live_boot_fetch(tmp_path):
    """The smoke test relies on live-boot fetching the squashfs over HTTP."""
    cmdline = build_kernel_cmdline(_spec(tmp_path))
    assert "boot=live" in cmdline
    assert "fetch=http://10.0.2.2:8000/fleetboot.squashfs" in cmdline
    assert "ip=dhcp" in cmdline


def test_cmdline_uses_serial_console_for_headless(tmp_path):
    cmdline = build_kernel_cmdline(_spec(tmp_path))
    assert "console=ttyS0" in cmdline


def test_qemu_command_uses_uefi_firmware(tmp_path):
    cmd = build_qemu_command(_spec(tmp_path))
    joined = " ".join(cmd)
    assert f"file={OVMF_CODE}" in joined
    assert "OVMF_VARS.fd" in joined
    assert "if=pflash" in joined


def test_qemu_command_passes_kernel_initrd_and_append(tmp_path):
    spec = _spec(tmp_path)
    cmd = build_qemu_command(spec)
    assert "-kernel" in cmd
    assert str(spec.kernel) in cmd
    assert "-initrd" in cmd
    assert str(spec.initrd) in cmd
    assert "-append" in cmd


def test_qemu_command_uses_user_mode_networking(tmp_path):
    cmd = build_qemu_command(_spec(tmp_path))
    joined = " ".join(cmd)
    # We rely on 10.0.2.2 = host. Confirm user-mode net is wired.
    assert "user,id=net0" in joined
    assert "virtio-net-pci,netdev=net0" in joined


def test_qemu_command_is_headless(tmp_path):
    cmd = build_qemu_command(_spec(tmp_path))
    assert "-nographic" in cmd
    assert "-no-reboot" in cmd


def test_qemu_command_logs_serial_when_path_given(tmp_path):
    spec = _spec(tmp_path)
    cmd = build_qemu_command(spec)
    joined = " ".join(cmd)
    assert f"file:{spec.serial_log}" in joined


def test_qemu_command_omits_serial_log_when_not_requested(tmp_path):
    spec = _spec(tmp_path)
    spec_no_log = QemuRunSpec(
        qemu_binary=spec.qemu_binary,
        kernel=spec.kernel,
        initrd=spec.initrd,
        fetch_url=spec.fetch_url,
        fleetboot_server_url=spec.fleetboot_server_url,
        boot_token=spec.boot_token,
        host_port=spec.host_port,
        vars_file=spec.vars_file,
        serial_log=None,
    )
    cmd = build_qemu_command(spec_no_log)
    joined = " ".join(cmd)
    assert "file:" not in joined
