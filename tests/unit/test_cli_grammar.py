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
import subprocess
import sys
from pathlib import Path
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

    @pytest.mark.parametrize("verb", ["webhook", "preflight"])
    def test_the_moved_verbs_are_advertised_under_the_noun(self, verb):
        """Hidden at the root, where they are the old spelling; listed under
        `tunnel`, where they are the new one. The same command object was wired
        in both places, so its `hidden=True` hid it from `hle tunnel --help` too."""
        result = CliRunner().invoke(main, ["tunnel", "--help"])
        assert result.exit_code == 0
        assert f"  {verb} " in result.output

    @pytest.mark.parametrize("verb", ["webhook", "preflight"])
    def test_the_moved_verbs_still_run_under_the_noun(self, verb):
        result = CliRunner().invoke(main, ["tunnel", verb, "--help"])
        assert result.exit_code == 0
        assert f"tunnel {verb} [OPTIONS]" in result.output

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


class TestTunnelHelpListsEveryMovedVerb:
    """The exact regression named in the audit (§6): `webhook`/`preflight` were

    wired under `tunnel` with the *same* (hidden) command object used at the
    root, so `hidden=True` hid them from `hle tunnel --help` too — the group
    that is supposed to be where they are the advertised spelling.
    """

    def test_tunnel_help_lists_webhook_and_preflight(self):
        result = CliRunner().invoke(main, ["tunnel", "--help"])
        assert result.exit_code == 0
        assert "webhook" in result.output
        assert "preflight" in result.output


class TestEveryAliasResolves:
    """Every alias in every `AliasedGroup` in the tree, not just the root's.

    `RootGroup`'s ``LEGACY_ALIASES`` (`cli.py:52`) and `config_group`'s
    ``show -> get`` (`config_cmd.py:212`) are two separate alias tables; a
    test that only walked one of them would miss a broken alias in the other.
    """

    def _all_aliased_groups(self) -> list[tuple[click.Group, str]]:
        found: list[tuple[click.Group, str]] = []

        def _visit(cmd: click.Command, path: str) -> None:
            if isinstance(cmd, click.Group):
                if getattr(cmd, "aliases", None):
                    found.append((cmd, path))
                for name, child in cmd.commands.items():
                    _visit(child, f"{path} {name}")

        _visit(main, "hle")
        return found

    def test_every_alias_resolves_to_its_named_target(self):
        groups = self._all_aliased_groups()
        assert groups, "expected at least one AliasedGroup with aliases"
        for group, path in groups:
            aliases: dict[str, str] = getattr(group, "aliases")  # noqa: B009
            for old, target in aliases.items():
                ctx = click.Context(group)
                resolved = group.get_command(ctx, old)
                assert resolved is not None, f"{path} : alias {old!r} -> {target!r} did not resolve"
                parts = target.split()
                expected: click.Command | None = group.get_command(ctx, parts[0])
                for part in parts[1:]:
                    assert isinstance(expected, click.Group)
                    expected = expected.get_command(ctx, part)
                assert resolved is expected, (
                    f"{path} : alias {old!r} resolved to a different object than "
                    f"looking up {target!r} directly"
                )


class TestReadmeCommandsParse:
    """The README's examples, checked the way the audit's script checks docs.

    `repos/hle/scripts/check-documented-commands.py` (server repo) parses
    every `hle ...` example in a set of files against the installed client,
    without running any of them, and says which ones the CLI would reject —
    this is exactly how the audit found the README teaching a dead command
    (`hle expose --service ... ` with no label, §"Bugs found"). That script
    lives in the proprietary server repo, not here, so it is only reachable
    from an orchestrator checkout that has both repos side by side; anywhere
    else (a bare `hle-client` clone, this package's own CI) it is simply
    absent, and this test says so instead of failing.
    """

    def _find_script(self) -> Path | None:
        # The expected layout when both repos are checked out side by side
        # under the orchestrator's `repos/` (see hle.orchistrator/CLAUDE.md).
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "hle" / "scripts" / "check-documented-commands.py"
            if candidate.is_file():
                return candidate
            candidate = parent / "repos" / "hle" / "scripts" / "check-documented-commands.py"
            if candidate.is_file():
                return candidate
        return None

    def test_readme_commands_pass_the_servers_doc_checker(self):
        script = self._find_script()
        if script is None:
            pytest.skip(
                "repos/hle/scripts/check-documented-commands.py not found — only "
                "available from an orchestrator checkout with both repos cloned "
                "side by side (see hle.orchistrator/CLAUDE.md's repository map); "
                "not reachable from a standalone hle-client checkout or its own CI."
            )
        readme = Path(__file__).resolve().parents[2] / "README.md"
        assert readme.is_file()
        result = subprocess.run(  # noqa: S603 — fixed argv, local script + file
            [sys.executable, str(script), str(readme)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
