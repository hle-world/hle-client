"""`hle tui` resolves its credential the way every built-in command does.

The dashboard is a plugin, so nothing obliges it to see the root's options.
It reads the root ``--api-key`` through ``hle_client.context`` anyway, because
``hle --api-key X tui`` otherwise silently fell back to the config file — the
same bug the shared context was introduced to fix for the built-ins.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner
from hle_client.cli import main

from hle_tui.plugin import tui

_KEY = "hle_" + "a" * 32
_LEAF_KEY = "hle_" + "b" * 32


def _run(argv: list[str]):
    # HleApp imports textual and starts an event loop; patched, this is just
    # where the resolved key is handed over.
    with patch("hle_tui.app.HleApp") as app_cls:
        result = CliRunner().invoke(main, argv)
    return result, app_cls


class TestRootApiKeyReachesTheDashboard:
    def test_the_root_context_key_is_used(self):
        result, app_cls = _run(["--api-key", _KEY, "tui", "--refresh", "0"])
        assert result.exit_code == 0, result.output
        assert app_cls.call_args.kwargs["api_key"] == _KEY

    def test_the_leaf_api_key_still_wins_over_the_root(self):
        result, app_cls = _run(["--api-key", _KEY, "tui", "--api-key", _LEAF_KEY, "--refresh", "0"])
        assert result.exit_code == 0, result.output
        assert app_cls.call_args.kwargs["api_key"] == _LEAF_KEY


class TestDirectInvocation:
    """The command tolerates a context the root never populated, like the rest."""

    def test_a_root_style_context_is_read(self):
        with patch("hle_tui.app.HleApp") as app_cls:
            result = CliRunner().invoke(tui, ["--refresh", "0"], obj={"api_key": _KEY})
        assert result.exit_code == 0, result.output
        assert app_cls.call_args.kwargs["api_key"] == _KEY
