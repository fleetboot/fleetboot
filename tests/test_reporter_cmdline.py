"""Tests for the kernel command-line parser used by the image-side reporter."""

import pytest

from fleetboot.reporter.cmdline import (
    MissingReporterSettingsError,
    parse_cmdline,
    read_settings,
)


def test_parses_well_formed_cmdline():
    line = (
        "BOOT_IMAGE=/vmlinuz ro quiet "
        "fleetboot.server=https://fleetboot.example/ "
        "fleetboot.boot_token=deadbeef "
        "console=tty0"
    )
    settings = parse_cmdline(line)
    assert settings.server_url == "https://fleetboot.example/"
    assert settings.boot_token == "deadbeef"


def test_ignores_unrelated_kernel_params():
    line = "ro quiet fleetboot.server=https://a/ fleetboot.boot_token=t"
    settings = parse_cmdline(line)
    assert settings.server_url == "https://a/"
    assert settings.boot_token == "t"


def test_missing_token_raises():
    with pytest.raises(MissingReporterSettingsError):
        parse_cmdline("fleetboot.server=https://a/")


def test_missing_server_raises():
    with pytest.raises(MissingReporterSettingsError):
        parse_cmdline("fleetboot.boot_token=t")


def test_completely_unrelated_cmdline_raises():
    with pytest.raises(MissingReporterSettingsError):
        parse_cmdline("BOOT_IMAGE=/vmlinuz ro quiet")


def test_read_settings_reads_from_a_file(tmp_path):
    path = tmp_path / "cmdline"
    path.write_text(
        "fleetboot.server=https://a/ fleetboot.boot_token=tok"
    )
    settings = read_settings(path)
    assert settings.server_url == "https://a/"
    assert settings.boot_token == "tok"
