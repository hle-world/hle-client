"""ops.daemon — the service backends, wrapped so they return something."""

from __future__ import annotations

from unittest.mock import patch

from hle_client.errors import HleError
from hle_client.ops import daemon

SPEC = {"label": "agent", "run_args": ["agent", "run"], "user_mode": True}


class TestListDaemons:
    async def test_lists_both_scopes_with_kind_and_state(self):
        with (
            patch(
                "hle_client.service_cmd.installed_services",
                return_value=[("hle-agent.service", True), ("hle-ha.service", False)],
            ),
            patch(
                "hle_client.service_cmd.service_spec",
                side_effect=[SPEC, {"label": "ha", "run_args": ["expose", "--service", "x"]}],
            ),
            patch("hle_client.ops.daemon._state", side_effect=["active", "inactive"]),
        ):
            found = await daemon.list_daemons()
        assert [(d.name, d.scope, d.kind, d.state) for d in found] == [
            ("hle-agent.service", "user", "agent", "active"),
            ("hle-ha.service", "system", "tunnel", "inactive"),
        ]

    async def test_scope_narrows(self):
        with (
            patch(
                "hle_client.service_cmd.installed_services",
                return_value=[("hle-agent.service", True), ("hle-ha.service", False)],
            ),
            patch("hle_client.service_cmd.service_spec", return_value=None),
            patch("hle_client.ops.daemon._state", return_value=None),
        ):
            found = await daemon.list_daemons(scope=False)
        assert [(d.name, d.kind) for d in found] == [("hle-ha.service", "unknown")]

    def test_state_survives_a_missing_manager(self):
        with (
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch("hle_client.ops.daemon.subprocess.run", side_effect=FileNotFoundError),
        ):
            assert daemon._state("hle-agent.service", False) is None


class TestServiceName:
    def test_per_platform(self):
        with patch("hle_client.service_cmd.current_platform", return_value="linux"):
            assert daemon.service_name("ha") == "hle-ha.service"
        with patch("hle_client.service_cmd.current_platform", return_value="darwin"):
            assert daemon.service_name("ha").startswith("world.hle")
        with patch("hle_client.service_cmd.current_platform", return_value="freebsd"):
            assert daemon.service_name("ha").startswith("hle_")


class TestRestartAndRefresh:
    async def test_restart_passes_the_scope_through(self):
        with patch("hle_client.service_cmd.restart_service", return_value=True) as restart:
            assert await daemon.restart("hle-agent.service", True) is True
        restart.assert_called_once_with("hle-agent.service", True)

    async def test_refresh_returns_the_outcome_word(self):
        with patch("hle_client.service_cmd.refresh_service", return_value="restarted") as refresh:
            assert await daemon.refresh("hle-agent.service", False) == "restarted"
        refresh.assert_called_once_with("hle-agent.service", False)


class TestInstallAndUninstall:
    async def test_install_goes_through_the_one_generator(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd._install_from_spec") as install,
        ):
            d = await daemon.install(SPEC, start=False)
        install.assert_called_once_with(SPEC, plat="linux", start=False)
        assert (d.name, d.scope, d.kind) == ("hle-agent.service", "user", "agent")

    async def test_uninstall_dispatches_per_platform(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="darwin"),
            patch("hle_client.service_cmd._launchd_uninstall") as un,
        ):
            await daemon.uninstall("agent", user_mode=True)
        un.assert_called_once_with(label="agent", name=None, user_mode=True)

    async def test_backend_errors_stay_typed(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd._systemd_uninstall", side_effect=HleError("denied")),
        ):
            try:
                await daemon.uninstall("agent", user_mode=False)
            except HleError as exc:
                assert str(exc) == "denied"
            else:
                raise AssertionError("expected HleError")
