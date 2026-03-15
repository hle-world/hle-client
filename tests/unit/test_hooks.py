"""Unit tests for hle_client.hooks — programmatic hook support."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from hle_client.hooks import HOOK_DEFINITIONS, HookRunner, parse_hooks

# ---------------------------------------------------------------------------
# parse_hooks
# ---------------------------------------------------------------------------


class TestParseHooks:
    def test_empty_input(self) -> None:
        assert parse_hooks(()) == {}

    def test_single_hook(self) -> None:
        result = parse_hooks(("tunnel_established=/usr/bin/my-script.sh",))
        assert result == {"tunnel_established": "/usr/bin/my-script.sh"}

    def test_multiple_hooks(self) -> None:
        result = parse_hooks(
            (
                "tunnel_established=/usr/bin/up.sh",
                "tunnel_dismantled=/usr/bin/down.sh",
            )
        )
        assert result == {
            "tunnel_established": "/usr/bin/up.sh",
            "tunnel_dismantled": "/usr/bin/down.sh",
        }

    def test_rejects_missing_equals(self) -> None:
        with pytest.raises(SystemExit, match="Invalid --hook format"):
            parse_hooks(("tunnel_established",))

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(SystemExit, match="Both hook name and script path"):
            parse_hooks(("=/usr/bin/script.sh",))

    def test_rejects_empty_script(self) -> None:
        with pytest.raises(SystemExit, match="Both hook name and script path"):
            parse_hooks(("tunnel_established=",))

    def test_rejects_unknown_hook(self) -> None:
        with pytest.raises(SystemExit, match="Unknown hook name"):
            parse_hooks(("made_up_event=/usr/bin/script.sh",))

    def test_rejects_duplicate_hook(self) -> None:
        with pytest.raises(SystemExit, match="Duplicate hook"):
            parse_hooks(
                (
                    "tunnel_established=/a.sh",
                    "tunnel_established=/b.sh",
                )
            )

    def test_script_path_with_equals(self) -> None:
        """Script path may contain '=' characters (e.g. env vars in path)."""
        result = parse_hooks(("tunnel_established=/opt/x=1/script.sh",))
        assert result == {"tunnel_established": "/opt/x=1/script.sh"}


# ---------------------------------------------------------------------------
# HOOK_DEFINITIONS
# ---------------------------------------------------------------------------


class TestHookDefinitions:
    def test_known_hooks(self) -> None:
        assert "tunnel_established" in HOOK_DEFINITIONS
        assert "tunnel_dismantled" in HOOK_DEFINITIONS

    def test_tunnel_established_args(self) -> None:
        assert HOOK_DEFINITIONS["tunnel_established"] == [
            "subdomain",
            "public_url",
            "tunnel_id",
        ]

    def test_tunnel_dismantled_args(self) -> None:
        assert HOOK_DEFINITIONS["tunnel_dismantled"] == [
            "subdomain",
            "public_url",
            "tunnel_id",
        ]


# ---------------------------------------------------------------------------
# HookRunner.fire
# ---------------------------------------------------------------------------


class TestHookRunnerFire:
    @pytest.mark.asyncio
    async def test_no_hooks_configured(self) -> None:
        """fire() with no hooks should be a no-op."""
        runner = HookRunner(hooks={})
        # Should not raise
        await runner.fire("tunnel_established", subdomain="x", public_url="u", tunnel_id="t")

    @pytest.mark.asyncio
    async def test_hook_not_configured_for_event(self) -> None:
        """fire() for an event with no registered script is a no-op."""
        runner = HookRunner(hooks={"tunnel_dismantled": "/bin/true"})
        await runner.fire("tunnel_established", subdomain="x", public_url="u", tunnel_id="t")

    @pytest.mark.asyncio
    async def test_fires_script_with_correct_args(self) -> None:
        """The hook script receives positional args in the order defined by HOOK_DEFINITIONS."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write('#!/bin/sh\necho "$@"\n')
            script = f.name
        os.chmod(script, stat.S_IRWXU)

        try:
            runner = HookRunner(hooks={"tunnel_established": script})
            # mock create_subprocess_exec to capture arguments
            with patch("hle_client.hooks.asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.communicate = AsyncMock(return_value=(b"", b""))
                mock_proc.returncode = 0
                mock_exec.return_value = mock_proc

                await runner.fire(
                    "tunnel_established",
                    subdomain="app-x7k",
                    public_url="https://app-x7k.hle.world",
                    tunnel_id="tid-123",
                )

                mock_exec.assert_called_once_with(
                    script,
                    "app-x7k",
                    "https://app-x7k.hle.world",
                    "tid-123",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        finally:
            os.unlink(script)

    @pytest.mark.asyncio
    async def test_logs_warning_on_nonzero_exit(self) -> None:
        runner = HookRunner(hooks={"tunnel_established": "/bin/false"})
        with patch("hle_client.hooks.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"oops"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            with patch("hle_client.hooks.logger") as mock_logger:
                await runner.fire(
                    "tunnel_established",
                    subdomain="x",
                    public_url="u",
                    tunnel_id="t",
                )
                mock_logger.warning.assert_called_once()
                assert "exited with code" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handles_file_not_found(self) -> None:
        runner = HookRunner(hooks={"tunnel_established": "/nonexistent/script.sh"})
        with (
            patch(
                "hle_client.hooks.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError,
            ),
            patch("hle_client.hooks.logger") as mock_logger,
        ):
            await runner.fire(
                "tunnel_established",
                subdomain="x",
                public_url="u",
                tunnel_id="t",
            )
            mock_logger.error.assert_called_once()
            assert "not found" in mock_logger.error.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handles_permission_error(self) -> None:
        runner = HookRunner(hooks={"tunnel_established": "/etc/shadow"})
        with (
            patch(
                "hle_client.hooks.asyncio.create_subprocess_exec",
                side_effect=PermissionError,
            ),
            patch("hle_client.hooks.logger") as mock_logger,
        ):
            await runner.fire(
                "tunnel_established",
                subdomain="x",
                public_url="u",
                tunnel_id="t",
            )
            mock_logger.error.assert_called_once()
            assert "permission denied" in mock_logger.error.call_args[0][0]

    @pytest.mark.asyncio
    async def test_missing_kwargs_default_to_empty_string(self) -> None:
        """Arguments not supplied in kwargs default to empty string."""
        runner = HookRunner(hooks={"tunnel_established": "/bin/echo"})
        with patch("hle_client.hooks.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await runner.fire("tunnel_established")  # No kwargs

            mock_exec.assert_called_once_with(
                "/bin/echo",
                "",
                "",
                "",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

    @pytest.mark.asyncio
    async def test_script_with_arguments_in_path(self) -> None:
        """Script path can contain arguments (e.g. '/bin/sh -c ...')."""
        runner = HookRunner(hooks={"tunnel_established": "/bin/sh -c echo"})
        with patch("hle_client.hooks.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await runner.fire(
                "tunnel_established",
                subdomain="x",
                public_url="u",
                tunnel_id="t",
            )

            # shlex.split('/bin/sh -c echo') + args
            mock_exec.assert_called_once_with(
                "/bin/sh",
                "-c",
                "echo",
                "x",
                "u",
                "t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
