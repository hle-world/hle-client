"""The CLI has to work on a box that cannot afford rich.

rich is the one dependency that buys appearance rather than function, and a
pfSense or OpenWrt device wants the tunnel, not a rendering library. It moved
to the `pretty` extra; this shim is what lets every call site keep writing
``console.print("[green]ok[/green]")`` either way.

These tests exercise the fallbacks directly, because a test run has rich
installed and would otherwise never see them.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def norich(monkeypatch):
    """Reimport richcompat with rich unavailable."""
    real_import = __import__

    def _fail_rich(name, *args, **kwargs):
        if name.startswith("rich"):
            raise ImportError("no rich here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fail_rich)
    monkeypatch.delitem(sys.modules, "hle_client.richcompat", raising=False)
    module = importlib.import_module("hle_client.richcompat")
    yield module
    # undo() restores the real module in sys.modules; every other module holds
    # a direct reference to the original Console/Table, so nothing needs a
    # reload here — and reloading would hand them a second, different class.
    monkeypatch.undo()


class TestWithoutRich:
    def test_it_reports_that_rich_is_missing(self, norich):
        assert norich.HAVE_RICH is False

    def test_console_prints_without_the_markup(self, norich, capsys):
        norich.Console().print("[green]Connected[/green] to the relay")
        assert capsys.readouterr().out.strip() == "Connected to the relay"

    def test_a_table_renders_as_aligned_columns(self, norich, capsys):
        table = norich.Table(title="Active Tunnels")
        table.add_column("Subdomain")
        table.add_column("URL")
        table.add_row("ha-x7k", "http://localhost:8123")
        table.add_row("jellyfin-x7k", "http://10.0.0.5:8096")
        norich.Console().print(table)

        printed = capsys.readouterr().out
        assert "Active Tunnels" in printed
        assert "Subdomain" in printed
        # The short name is padded to the width of the long one, so the second
        # column starts at the same offset on every row.
        rows = [line for line in printed.splitlines() if "http" in line]
        assert len({line.index("http") for line in rows}) == 1

    def test_rich_styling_keywords_are_accepted_and_ignored(self, norich):
        """Call sites pass rich's kwargs; the fallback must not choke on them."""
        table = norich.Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("Subdomain", "ha-x7k")
        assert "ha-x7k" in table.render()

    def test_a_multiline_cell_does_not_break_the_layout(self, norich):
        """Access rules arrive joined with newlines."""
        table = norich.Table()
        table.add_column("Rules")
        table.add_row("a@example.com (google)\nb@example.com (github)")
        rendered = table.render()
        assert "a@example.com" in rendered
        assert "b@example.com" in rendered

    def test_an_empty_table_renders_nothing_alarming(self, norich):
        table = norich.Table()
        assert isinstance(table.render(), str)
