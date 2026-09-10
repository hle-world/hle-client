"""The CLI has one shape: `hle <noun> <verb>`, and the old shapes still work.

Four grammars coexisted at the top level — bare verbs (`expose`), an
abbreviation (`fp`), noun-verb (`agent list`), and a noun that named the wrong
thing (`config` for tunnels, while `auth login` wrote the actual client
config). There was no rule to learn, so every command had to be looked up.

These tests pin both halves of the fix: the new grammar is what `--help`
teaches, and every old spelling still resolves, because units, scripts and
years of support answers are written against them.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from hle_client import __version__
from hle_client.aliases import hoist_global_options
from hle_client.cli import main


def _ctx() -> click.Context:
    return click.Context(main)


class TestOneGrammar:
    @pytest.mark.parametrize("noun", ["tunnel", "agent", "daemon", "forward", "auth"])
    def test_the_nouns_are_what_help_lists(self, noun):
        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0
        assert noun in result.output

    @pytest.mark.parametrize("old", ["expose", "webhook", "preflight", "config", "service", "fp"])
    def test_the_old_spellings_are_not_advertised(self, old):
        """Hidden, not removed. Help teaches one shape."""
        result = CliRunner().invoke(main, ["--help"])
        assert f"  {old} " not in result.output

    @pytest.mark.parametrize(
        ("old", "new"),
        [("config", "tunnel"), ("service", "daemon"), ("fp", "forward")],
    )
    def test_the_old_group_names_still_resolve(self, old, new):
        assert main.get_command(_ctx(), old) is main.commands[new]

    @pytest.mark.parametrize("old", ["expose", "webhook", "preflight"])
    def test_the_old_commands_still_run(self, old):
        assert main.get_command(_ctx(), old) is not None

    def test_an_unknown_command_is_still_unknown(self):
        """The alias table must not turn typos into silent successes."""
        assert main.get_command(_ctx(), "explode") is None


class TestVersionIsAskable:
    """`hle version` answered with a usage error, in a CLI whose whole claim is
    that everything is a word you can guess. `--version` existed; nobody types
    it first."""

    def test_version_prints_the_version(self):
        result = CliRunner().invoke(main, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_version_answers_json_like_every_other_read(self):
        result = CliRunner().invoke(main, ["version", "-o", "json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"version": __version__}

    def test_the_flag_still_works(self):
        result = CliRunner().invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output


class TestDeprecationNotice:
    def test_using_an_old_name_says_what_to_type(self):
        from hle_client import aliases

        aliases._warned.clear()
        result = CliRunner().invoke(main, ["config", "--help"])
        assert result.exit_code == 0
        assert "hle config" in result.stderr
        assert "hle tunnel" in result.stderr

    def test_the_notice_goes_to_stderr(self):
        """A script parsing stdout must not start seeing advice in its data."""
        from hle_client import aliases

        aliases._warned.clear()
        result = CliRunner().invoke(main, ["config", "--help"])
        assert "note:" not in result.stdout


class TestTunnelIsANoun:
    """The central object had no name; `config` was doing duty for it."""

    def test_create_takes_its_label_and_url_as_arguments(self):
        """Not as required flags. `--service X --label ha` was the long way."""
        create = main.commands["tunnel"].commands["create"]
        args = [p for p in create.params if isinstance(p, click.Argument)]
        assert len(args) == 2
        assert "[LABEL] URL" in create.collect_usage_pieces(click.Context(create))

    def test_a_url_on_its_own_is_read_as_the_url(self):
        """Click fills an optional argument before a required one.

        Declared as `[LABEL] URL`, `hle tunnel create http://localhost:8080`
        failed with "Missing argument URL" — the label-less form the docs have
        always shown, and the one --apex needs.
        """
        from hle_client.cli import expose

        with patch.object(expose, "callback") as impl:
            result = CliRunner().invoke(main, ["tunnel", "create", "http://localhost:8080"])
        assert result.exit_code == 0, result.output
        assert impl.call_args.kwargs["service"] == "http://localhost:8080"
        assert impl.call_args.kwargs["service_label"] is None

    def test_two_arguments_are_label_then_url(self):
        from hle_client.cli import expose

        with patch.object(expose, "callback") as impl:
            CliRunner().invoke(main, ["tunnel", "create", "ha", "http://localhost:8123"])
        assert impl.call_args.kwargs["service_label"] == "ha"
        assert impl.call_args.kwargs["service"] == "http://localhost:8123"

    @pytest.mark.parametrize("verb", ["create", "list", "get", "delete", "webhook", "preflight"])
    def test_the_verbs_live_under_the_noun(self, verb):
        assert verb in main.commands["tunnel"].commands

    def test_show_still_works_as_get(self):
        tunnel = main.commands["tunnel"]
        ctx = click.Context(tunnel)
        assert tunnel.get_command(ctx, "show") is tunnel.commands["get"]


class TestGlobalFlags:
    """Declared once at the root, not re-declared on fifteen leaves."""

    @pytest.mark.parametrize(
        "flag", ["--api-key", "--output", "--quiet", "--no-color", "--no-input", "--debug"]
    )
    def test_the_root_declares_it(self, flag):
        result = CliRunner().invoke(main, ["--help"])
        assert flag in result.output

    def test_output_accepts_json(self):
        result = CliRunner().invoke(main, ["--help"])
        assert "json" in result.output


class TestGlobalFlagsWorkWhereTheyAreTyped:
    """Click parses root options only before the subcommand. Nobody types them there.

    `hle status -o json` handed `-o json` to a subcommand with no such option,
    so it silently printed the human view — the worst outcome, because a script
    consuming it looks like it works until it doesn't.
    """

    def test_a_trailing_option_moves_to_the_front(self):
        assert hoist_global_options(["status", "-o", "json"]) == ["-o", "json", "status"]

    def test_an_equals_form_moves_too(self):
        assert hoist_global_options(["tunnel", "list", "--output=json"]) == [
            "--output=json",
            "tunnel",
            "list",
        ]

    def test_a_bare_flag_moves(self):
        assert hoist_global_options(["tunnel", "list", "--quiet"]) == ["--quiet", "tunnel", "list"]

    def test_arguments_keep_their_order(self):
        assert hoist_global_options(["tunnel", "create", "ha", "http://x", "-q"]) == [
            "-q",
            "tunnel",
            "create",
            "ha",
            "http://x",
        ]

    def test_nothing_after_a_double_dash_is_touched(self):
        """`hle forward box 10.0.0.5:22 -- ssh -q me@host` passes -q to ssh."""
        args = ["forward", "box", "10.0.0.5:22", "--", "ssh", "-q", "me@host"]
        assert hoist_global_options(args) == args

    def test_a_value_is_carried_with_its_option(self):
        assert hoist_global_options(["status", "--output", "json"]) == [
            "--output",
            "json",
            "status",
        ]

    def test_an_api_key_is_left_where_it_was_typed(self):
        """On `auth login` it names the key to save, not one to authenticate with.

        Hoisting it took the argument away from the command, which then
        prompted for a key the user had already supplied.
        """
        args = ["auth", "login", "--api-key", "hle_x"]
        assert hoist_global_options(args) == args
