"""`hle forward <agent> <target>` — the flags were arguments in disguise.

`--agent` and `--to` were both required, which is a flag you must always pass:
an argument wearing a costume. They still work, because they are in scripts and
unit files, but the documented form is positional now.

The subtlety is the trailing command. `hle forward rpi 22 -- ssh …` puts the
agent, the targets and the command in one bucket, and only the `--` separates
them.
"""

from __future__ import annotations

import pytest

from hle_client.fp_cmd import _split_positionals


class TestPositionalForm:
    def test_the_first_argument_is_the_agent(self):
        agent, targets, command = _split_positionals(None, (), ("rpi", "22"))
        assert agent == "rpi"
        assert targets == ("22",)
        assert command == ()

    def test_every_further_argument_is_a_target(self):
        agent, targets, _ = _split_positionals(None, (), ("rpi", "22", "nas:445"))
        assert agent == "rpi"
        assert targets == ("22", "nas:445")

    def test_a_host_port_target_survives_intact(self):
        _, targets, _ = _split_positionals(None, (), ("nas", "192.168.1.50:5432"))
        assert targets == ("192.168.1.50:5432",)


class TestTheTrailingCommand:
    def test_everything_after_the_separator_is_the_command(self):
        agent, targets, command = _split_positionals(
            None, (), ("rpi", "22", "--", "ssh", "-p", "{port}", "me@127.0.0.1")
        )
        assert agent == "rpi"
        assert targets == ("22",)
        assert command == ("--", "ssh", "-p", "{port}", "me@127.0.0.1")

    def test_a_command_that_looks_like_a_target_is_not_taken_as_one(self):
        _, targets, command = _split_positionals(None, (), ("rpi", "22", "--", "./backup.sh"))
        assert targets == ("22",)
        assert command == ("--", "./backup.sh")


class TestTheOldFlagFormIsUntouched:
    def test_both_flags_leave_every_positional_to_the_command(self):
        """`hle fp --agent rpi --to 22 -- ssh ...` must not lose its command."""
        agent, targets, command = _split_positionals("rpi", ("22",), ("--", "ssh", "me@host"))
        assert agent == "rpi"
        assert targets == ("22",)
        assert command == ("--", "ssh", "me@host")

    def test_an_explicit_agent_with_positional_targets(self):
        agent, targets, _ = _split_positionals("rpi", (), ("22", "5432"))
        assert agent == "rpi"
        assert targets == ("22", "5432")

    def test_an_explicit_target_with_a_positional_agent(self):
        agent, targets, _ = _split_positionals(None, ("22",), ("rpi",))
        assert agent == "rpi"
        assert targets == ("22",)


class TestMissingPieces:
    @pytest.mark.parametrize(
        ("agent", "targets", "command"),
        [(None, (), ()), (None, (), ("--", "ls"))],
    )
    def test_nothing_is_invented(self, agent, targets, command):
        resolved_agent, resolved_targets, _ = _split_positionals(agent, targets, command)
        assert resolved_agent is None
        assert resolved_targets == ()


class TestTheDocumentedExamplesParse:
    """Every example in the help text, run through the parser."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["rpi", "22", "--port", "9922"],
            ["nas", "192.168.1.50:5432"],
            ["rpi", "22", "nas:445"],
            ["rpi", "22", "--", "ssh", "-p", "{port}", "me@127.0.0.1"],
            ["rpi", "22", "5432", "--", "./backup.sh"],
        ],
    )
    def test_it_resolves_an_agent_and_a_target(self, argv):
        from unittest.mock import patch

        from click.testing import CliRunner

        from hle_client.cli import main

        # No credential anywhere, so the command stops at the auth check —
        # which is exactly far enough to prove the arguments were understood,
        # and not so far that the test needs a relay.
        with patch("hle_client.fp_cmd._load_api_key", return_value=None):
            result = CliRunner().invoke(main, ["forward", *argv], env={"HLE_API_KEY": ""})
        assert "No agent given" not in result.output
        assert "No target given" not in result.output
        assert "No API key" in result.output
