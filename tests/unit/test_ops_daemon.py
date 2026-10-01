"""ops.daemon — the service backends, wrapped so they return something."""

from __future__ import annotations

import plistlib
from pathlib import Path
from unittest.mock import patch

import pytest

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


class _FakeProc:
    def __init__(self, out: bytes = b"", err: bytes = b"", returncode: int = 0) -> None:
        self._out = out
        self._err = err
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err


class TestLogPath:
    def test_systemd_has_no_file(self):
        with patch("hle_client.service_cmd.current_platform", return_value="linux"):
            assert daemon.log_path("hle-ha.service") is None

    def test_rcd_reads_var_log(self):
        with patch("hle_client.service_cmd.current_platform", return_value="freebsd"):
            assert daemon.log_path("hle_ha") == Path("/var/log/hle_ha.log")

    def test_launchd_reads_the_path_out_of_the_plist(self, tmp_path):
        plist = tmp_path / "world.hle.ha.plist"
        plist.write_bytes(plistlib.dumps({"StandardOutPath": "/somewhere/ha.log"}))
        with (
            patch("hle_client.service_cmd.current_platform", return_value="darwin"),
            patch("hle_client.service_cmd.service_file", return_value=plist),
        ):
            assert daemon.log_path("world.hle.ha") == Path("/somewhere/ha.log")

    def test_launchd_falls_back_to_the_log_dir(self, tmp_path):
        with (
            patch("hle_client.service_cmd.current_platform", return_value="darwin"),
            patch("hle_client.service_cmd.service_file", return_value=None),
            patch("hle_client.service_cmd._launchd_log_dir", return_value=str(tmp_path)),
        ):
            assert daemon.log_path("world.hle.ha", user_mode=False) == tmp_path / "ha.log"


class TestLogTail:
    async def test_systemd_asks_the_journal_for_the_unit(self):
        proc = _FakeProc(out=b"a\nb\nc\n")
        with (
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch(
                "hle_client.ops.daemon.asyncio.create_subprocess_exec", return_value=proc
            ) as exec_,
        ):
            tail = await daemon.log_tail("hle-ha.service", lines=2)
        assert tail == "b\nc"
        assert exec_.call_args.args == (
            "journalctl",
            "-u",
            "hle-ha.service",
            "-n",
            "2",
            "--no-pager",
        )

    async def test_systemd_user_scope_asks_the_user_journal(self):
        proc = _FakeProc(out=b"x\n")
        with (
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch(
                "hle_client.ops.daemon.asyncio.create_subprocess_exec", return_value=proc
            ) as exec_,
        ):
            await daemon.log_tail("hle-agent.service", user_mode=True)
        assert exec_.call_args.args[1] == "--user"

    async def test_missing_journalctl_is_typed(self):
        with (
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch(
                "hle_client.ops.daemon.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError,
            ),
            pytest.raises(HleError),
        ):
            await daemon.log_tail("hle-ha.service")

    async def test_journalctl_failure_carries_its_stderr(self):
        proc = _FakeProc(err=b"no such unit", returncode=1)
        with (
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch("hle_client.ops.daemon.asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(HleError) as err,
        ):
            await daemon.log_tail("hle-ha.service")
        assert "no such unit" in str(err.value)

    async def test_a_file_is_tailed_to_the_cap(self, tmp_path):
        log = tmp_path / "hle_ha.log"
        log.write_text("\n".join(f"line {i}" for i in range(500)))
        with patch("hle_client.ops.daemon.log_path", return_value=log):
            text = await daemon.log_tail("hle_ha", lines=3)
        assert text == "line 497\nline 498\nline 499"

    async def test_a_missing_file_is_an_error_with_a_hint(self, tmp_path):
        with (
            patch("hle_client.ops.daemon.log_path", return_value=tmp_path / "nope.log"),
            pytest.raises(HleError) as err,
        ):
            await daemon.log_tail("hle_ha")
        assert "No log file at" in str(err.value)
        assert "hle daemon status" in (err.value.hint or "")
