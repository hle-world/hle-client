"""Tests for the `hle update` self-upgrade logic."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hle_client.cli import main
from hle_client.update_cmd import (
    BREW,
    DOCKER,
    EDITABLE,
    EXTERNALLY_MANAGED,
    HA_ADDON,
    KUBERNETES,
    PIP,
    PIPX,
    UV,
    VENV,
    build_upgrade_command,
    detect_install_method,
    is_editable_install,
    is_externally_managed,
)


@pytest.fixture
def not_editable():
    """The suite itself runs from an editable install (CI does ``pip install -e``).

    Tests that classify a made-up system prefix must not see the real checkout.
    """
    with patch("hle_client.update_cmd.is_editable_install", return_value=False):
        yield


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

    def test_docker_from_dockerenv(self, not_editable, tmp_path):
        """The hle-docker image pip-installs into the system Python."""
        prefix = "/usr/local"
        method = detect_install_method(
            prefix,
            f"{prefix}/bin/python",
            base_prefix=prefix,
            stdlib=str(tmp_path / "lib"),
            env={},
            dockerenv=True,
        )
        assert method == DOCKER

    def test_docker_from_hle_in_docker(self, not_editable, tmp_path):
        prefix = "/usr/local"
        method = detect_install_method(
            prefix,
            f"{prefix}/bin/python",
            base_prefix=prefix,
            stdlib=str(tmp_path / "lib"),
            env={"HLE_IN_DOCKER": "1"},
        )
        assert method == DOCKER

    def test_dev_container_with_pipx_is_not_docker(self):
        """/.dockerenv alone must not hide a pipx install inside a dev container."""
        prefix = "/root/.local/share/pipx/venvs/hle-client"
        method = detect_install_method(prefix, f"{prefix}/bin/python", env={}, dockerenv=True)
        assert method == PIPX

    def test_dev_pod_with_uv_tool_is_not_kubernetes(self):
        prefix = "/root/.local/share/uv/tools/hle-client"
        env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}
        assert detect_install_method(prefix, f"{prefix}/bin/python", env=env) == UV

    def test_ha_addon_from_supervisor_token(self):
        prefix = "/usr"
        method = detect_install_method(
            prefix, f"{prefix}/bin/python3", env={"SUPERVISOR_TOKEN": "x"}
        )
        assert method == HA_ADDON

    def test_ha_addon_from_hassio(self):
        prefix = "/usr"
        method = detect_install_method(prefix, f"{prefix}/bin/python3", env={"HASSIO": "1"})
        assert method == HA_ADDON

    def test_kubernetes_from_service_host(self, not_editable, tmp_path):
        prefix = "/usr"
        method = detect_install_method(
            prefix,
            f"{prefix}/bin/python3",
            base_prefix=prefix,
            stdlib=str(tmp_path / "lib"),
            env={"KUBERNETES_SERVICE_HOST": "10.0.0.1"},
        )
        assert method == KUBERNETES

    def test_a_platform_beats_docker(self, not_editable, tmp_path):
        """An HA add-on and a k8s pod are containers too; name the outer one."""
        env = {"SUPERVISOR_TOKEN": "x", "KUBERNETES_SERVICE_HOST": "10.0.0.1"}
        probe = {"base_prefix": "/usr", "stdlib": str(tmp_path / "lib")}
        method = detect_install_method("/usr", "/usr/bin/python3", env=env, dockerenv=True, **probe)
        assert method == HA_ADDON
        assert (
            detect_install_method(
                "/usr",
                "/usr/bin/python3",
                env={"KUBERNETES_SERVICE_HOST": "x"},
                dockerenv=True,
                **probe,
            )
            == KUBERNETES
        )

    def test_install_method_env_override_wins(self):
        prefix = "/root/.local/share/hle/venv"
        env = {"HLE_INSTALL_METHOD": "kubernetes"}
        assert detect_install_method(prefix, f"{prefix}/bin/python", env=env) == KUBERNETES

    def test_an_unknown_env_override_is_ignored(self):
        prefix = "/root/.local/share/hle/venv"
        env = {"HLE_INSTALL_METHOD": "nonsense"}
        assert detect_install_method(prefix, f"{prefix}/bin/python", env=env) == VENV

    def test_editable_install_is_refused(self, tmp_path):
        prefix = tmp_path / "venv"
        info = prefix / "lib" / "python3.12" / "site-packages" / "hle_client-2609.10.dist-info"
        info.mkdir(parents=True)
        (info / "direct_url.json").write_text(
            '{"url": "file:///src/hle-client", "dir_info": {"editable": true}}'
        )
        assert is_editable_install(str(prefix)) is True
        assert detect_install_method(str(prefix), f"{prefix}/bin/python", env={}) == EDITABLE

    def test_a_regular_direct_url_is_not_editable(self, tmp_path):
        prefix = tmp_path / "venv"
        info = prefix / "lib" / "python3.12" / "site-packages" / "hle_client-2609.10.dist-info"
        info.mkdir(parents=True)
        (prefix / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (info / "direct_url.json").write_text(
            '{"url": "https://pypi.org/...", "archive_info": {"hash": "sha256=x"}}'
        )
        assert is_editable_install(str(prefix)) is False
        assert detect_install_method(str(prefix), f"{prefix}/bin/python", env={}) == VENV


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

    @pytest.mark.parametrize(
        ("method", "expected"),
        [
            (DOCKER, "docker compose pull"),
            (HA_ADDON, "Add-ons"),
            (KUBERNETES, "helm upgrade"),
            (EDITABLE, "git pull"),
        ],
    )
    def test_managed_installs_say_who_owns_the_version(self, method, expected):
        result, mock_run = self._run(method)
        assert result.exit_code == 1
        assert expected in result.output
        mock_run.assert_not_called()

    def test_check_still_works_on_docker(self):
        """`--check` only reads PyPI, so a container install is not refused."""
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value="9999.1"),
            patch("hle_client.update_cmd.detect_install_method", return_value=DOCKER),
        ):
            result = CliRunner().invoke(main, ["update", "--check"])
        assert result.exit_code == 0, result.output
        assert "9999.1" in result.output
        assert "docker compose pull" not in result.output

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
