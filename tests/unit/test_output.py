"""Output is one decision, made once — colour, quiet, and machine format.

Before this, every command built its own Console, three of about twenty-five
offered machine output, and colour was unconditional: no NO_COLOR, no check for
whether a terminal was attached. Piping `hle config list` into a file wrote
escape codes into it.

rich is optional here too, so a device that cannot afford it still gets every
command — just as plain text.
"""

from __future__ import annotations

import json

import pytest

from hle_client.output import JSON, TABLE, Output, color_enabled, strip_markup


class TestMarkupStripping:
    """Without rich, tags must go — but only tags."""

    def test_style_tags_are_removed(self):
        assert strip_markup("[green]ok[/green]") == "ok"
        assert strip_markup("[bold red]bad[/bold red]") == "bad"
        assert strip_markup("[dim]x[/]") == "x"

    def test_text_that_merely_looks_like_a_tag_survives(self):
        """A bare "[...]" regex ate log lines and IPv6 addresses."""
        assert strip_markup("worker[1] failed") == "worker[1] failed"
        assert strip_markup("http://[::1]:8080") == "http://[::1]:8080"
        assert strip_markup("[2026-09-07] done") == "[2026-09-07] done"


class TestColorDecision:
    def test_no_color_env_wins_over_a_tty(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert color_enabled(None, _FakeTty(True)) is False

    def test_a_pipe_is_never_coloured(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert color_enabled(None, _FakeTty(False)) is False

    def test_an_explicit_choice_wins(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert color_enabled(True, _FakeTty(False)) is True


class _FakeTty:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestJsonMode:
    def test_data_is_the_only_thing_on_stdout(self, capsys):
        """-o json means a parseable stream, so commentary is suppressed."""
        out = Output(fmt=JSON)
        out.print("Connecting to the relay...")
        out.data({"subdomain": "ha-x7k"})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"subdomain": "ha-x7k"}

    def test_nothing_is_emitted_in_table_mode(self, capsys):
        out = Output(fmt=TABLE)
        out.data({"subdomain": "ha-x7k"})
        assert capsys.readouterr().out == ""

    def test_errors_still_reach_stderr(self, capsys):
        """A silent failure is the complaint --quiet users actually have."""
        out = Output(fmt=JSON, quiet=True)
        out.error("Invalid or missing API key.")
        captured = capsys.readouterr()
        assert "Invalid or missing API key." in captured.err
        assert captured.out == ""


class TestQuiet:
    def test_commentary_is_suppressed(self, capsys):
        out = Output(quiet=True)
        out.print("Connecting...")
        assert capsys.readouterr().out == ""

    def test_the_answer_itself_is_not(self, capsys):
        out = Output(quiet=True)
        out.print("ha-x7k.hle.world", force=True)
        assert "ha-x7k.hle.world" in capsys.readouterr().out


class TestTableWithoutRich:
    def test_rows_are_aligned_columns(self, monkeypatch, capsys):
        monkeypatch.setattr("hle_client.output.HAVE_RICH", False)
        out = Output(color=False)
        monkeypatch.setattr(out, "_console", None)
        out.table(["Subdomain", "URL"], [["ha-x7k", "http://localhost:8123"]])
        printed = capsys.readouterr().out
        assert "Subdomain" in printed
        assert "ha-x7k" in printed
        assert "http://localhost:8123" in printed

    def test_markup_does_not_leak_into_plain_output(self, monkeypatch, capsys):
        monkeypatch.setattr("hle_client.output.HAVE_RICH", False)
        out = Output(color=False)
        monkeypatch.setattr(out, "_console", None)
        out.table(["State"], [["[green]live[/green]"]])
        assert "[green]" not in capsys.readouterr().out


@pytest.mark.parametrize("fmt", [TABLE, JSON])
def test_every_format_is_accepted(fmt):
    assert Output(fmt=fmt).fmt == fmt
