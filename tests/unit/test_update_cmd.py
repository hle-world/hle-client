"""Tests for the `hle update` self-upgrade logic."""

from __future__ import annotations

from hle_client.update_cmd import (
    PIP,
    PIPX,
    UV,
    VENV,
    build_upgrade_command,
    detect_install_method,
)


class TestDetectInstallMethod:
    def test_pipx(self):
        prefix = "/root/.local/share/pipx/venvs/hle-client"
        assert detect_install_method(prefix, f"{prefix}/bin/python") == PIPX

    def test_uv_tool(self):
        prefix = "/root/.local/share/uv/tools/hle-client"
        assert detect_install_method(prefix, f"{prefix}/bin/python") == UV

    def test_installer_plain_venv(self):
        prefix = "/root/.local/share/hle/venv"
        assert detect_install_method(prefix, f"{prefix}/bin/python") == VENV

    def test_installer_plain_venv_trailing_slash(self):
        prefix = "/home/ian/.local/share/hle/venv/"
        assert detect_install_method(prefix, f"{prefix}bin/python") == VENV

    def test_system_pip(self):
        # base prefix == prefix (no venv) → system pip
        import sys

        assert detect_install_method(sys.base_prefix, sys.executable) == PIP


class TestBuildUpgradeCommand:
    def test_pipx_latest(self):
        assert build_upgrade_command(PIPX, "/x/python") == ["pipx", "upgrade", "hle-client"]

    def test_pipx_pinned_forces_reinstall(self):
        assert build_upgrade_command(PIPX, "/x/python", version="2607.2") == [
            "pipx",
            "install",
            "--force",
            "hle-client==2607.2",
        ]

    def test_uv_latest(self):
        assert build_upgrade_command(UV, "/x/python") == ["uv", "tool", "upgrade", "hle-client"]

    def test_venv_uses_running_interpreter_pip(self):
        exe = "/root/.local/share/hle/venv/bin/python"
        assert build_upgrade_command(VENV, exe) == [
            exe,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "hle-client",
        ]

    def test_pip_pinned_version(self):
        exe = "/usr/bin/python3"
        assert build_upgrade_command(PIP, exe, version="2607.2") == [
            exe,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "hle-client==2607.2",
        ]


class TestUpdateCommandWiring:
    def test_registered_on_cli(self):
        from hle_client.cli import main

        assert "update" in main.commands
