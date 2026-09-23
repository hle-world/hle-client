"""Tests for the `hle update` self-upgrade logic."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from hle_client.cli import main
from hle_client.update_cmd import (
    BREW,
    EXTERNALLY_MANAGED,
    PIP,
    PIPX,
    UV,
    VENV,
    build_upgrade_command,
    detect_install_method,
    is_externally_managed,
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

    def test_system_pip(self, tmp_path):
        # base prefix == prefix (no venv), no PEP 668 marker → system pip
        prefix = str(tmp_path / "usr")
        method = detect_install_method(
            prefix, f"{prefix}/bin/python3", base_prefix=prefix, stdlib=str(tmp_path / "lib")
        )
        assert method == PIP

    def test_other_venv_needs_a_real_pyvenv_cfg(self, tmp_path):
        """The check used to stringify the path and never test it, so every
        non-venv interpreter with prefix != base_prefix was 'venv'."""
        prefix = tmp_path / "somewhere"
        prefix.mkdir()
        exe = str(prefix / "bin" / "python")
        kwargs = {"base_prefix": "/usr", "stdlib": str(tmp_path / "lib")}
        assert detect_install_method(str(prefix), exe, **kwargs) == PIP
        (prefix / "pyvenv.cfg").write_text("home = /usr/bin\n")
        assert detect_install_method(str(prefix), exe, **kwargs) == VENV

    def test_homebrew_keg(self, tmp_path):
        """A keg is a venv, but brew's. pip upgrading inside it corrupts it."""
        prefix = tmp_path / "opt/homebrew/Cellar/hle-client/2609.1/libexec"
        prefix.mkdir(parents=True)
        (prefix / "pyvenv.cfg").write_text("home = /opt/homebrew/bin\n")
        exe = str(prefix / "bin" / "python")
        assert detect_install_method(str(prefix), exe, base_prefix="/opt/homebrew") == BREW

    def test_externally_managed_system_python(self, tmp_path):
        stdlib = tmp_path / "lib" / "python3.12"
        stdlib.mkdir(parents=True)
        (stdlib / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
        prefix = str(tmp_path)
        method = detect_install_method(
            prefix, f"{prefix}/bin/python3", base_prefix=prefix, stdlib=str(stdlib)
        )
        assert method == EXTERNALLY_MANAGED
        assert is_externally_managed(str(stdlib)) is True
        assert is_externally_managed(str(tmp_path)) is False


class TestUpdateRefusesWhereItWouldBreakThings:
    def _run(self, method: str):
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value="9999.1"),
            patch("hle_client.update_cmd.detect_install_method", return_value=method),
            patch("hle_client.update_cmd.subprocess.run") as mock_run,
        ):
            result = CliRunner().invoke(main, ["update", "--yes"])
        return result, mock_run

    def test_brew_says_to_use_brew_and_installs_nothing(self):
        result, mock_run = self._run(BREW)
        assert result.exit_code == 1
        assert "brew upgrade hle-client" in result.output
        mock_run.assert_not_called()

    def test_externally_managed_installs_nothing(self):
        result, mock_run = self._run(EXTERNALLY_MANAGED)
        assert result.exit_code == 1
        assert "PEP 668" in result.output
        mock_run.assert_not_called()

    def test_check_still_works_on_brew(self):
        """`--check` reads PyPI and prints; it never installs, so no refusal."""
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value="9999.1"),
            patch("hle_client.update_cmd.detect_install_method", return_value=BREW),
        ):
            result = CliRunner().invoke(main, ["update", "--check"])
        assert result.exit_code == 0, result.output
        assert "9999.1" in result.output


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
