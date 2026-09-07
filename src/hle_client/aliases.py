"""Keep every old spelling working, quietly, forever.

The command names changed to one grammar — ``hle <noun> <verb>``. Scripts,
systemd units, blog posts and support answers written against the old names
did not change with them, and a CLI that breaks those has not improved for the
person it broke.

So the old names still resolve. They are hidden from ``--help``, so the shape
being taught is the new one, and they print a single line to stderr naming the
replacement — stderr because a script parsing stdout must not start seeing
advice mixed into its data.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import click

# Printed once per process. A loop calling `hle config list` in a script would
# otherwise emit the same nag on every iteration.
_warned: set[str] = set()


def warn_once(old: str, new: str) -> None:
    """Tell the user what to type instead, once."""
    if old in _warned or os.environ.get("HLE_NO_DEPRECATION_WARNINGS"):
        return
    _warned.add(old)
    print(f"note: `hle {old}` is now `hle {new}`. The old name still works.", file=sys.stderr)


# Options declared on the root group, which Click will only parse *before* the
# subcommand name. Nobody types them there: `hle status -o json` is the natural
# form, and it silently did nothing, because Click handed `-o json` to a
# subcommand that has no such option.
# `--api-key` is deliberately absent. On `auth login` it names the key to
# save, not the key to authenticate with — hoisting it to the root took the
# argument away from the command and left it prompting for one.
_GLOBAL_WITH_VALUE = {"-o", "--output"}
_GLOBAL_FLAGS = {"-q", "--quiet", "--no-color", "--no-input", "--debug"}


def hoist_global_options(args: list[str]) -> list[str]:
    """Move root-level options to the front, wherever the user put them.

    Everything after ``--`` is left exactly as it is: ``hle forward box
    10.0.0.5:22 -- ssh -q me@host`` passes ``-q`` to ssh, and it must keep
    doing that.
    """
    hoisted: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            rest.extend(args[i:])
            break
        if arg in _GLOBAL_WITH_VALUE and i + 1 < len(args):
            hoisted.extend([arg, args[i + 1]])
            i += 2
            continue
        if arg.split("=", 1)[0] in _GLOBAL_WITH_VALUE and "=" in arg:
            hoisted.append(arg)
            i += 1
            continue
        if arg in _GLOBAL_FLAGS:
            hoisted.append(arg)
            i += 1
            continue
        rest.append(arg)
        i += 1
    return hoisted + rest


class AliasedGroup(click.Group):
    """A group whose legacy command names resolve to their replacements.

    Resolution happens in ``get_command`` rather than by registering the same
    command twice, so the alias cannot drift from the command it points at and
    ``--help`` lists each command exactly once.
    """

    def __init__(self, *args: Any, aliases: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.aliases: dict[str, str] = aliases or {}

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command

        target = self.aliases.get(cmd_name)
        if target is None:
            return None

        # The alias may name a path ("tunnel webhook"), not just a command.
        parts = target.split()
        resolved: click.Command | None = super().get_command(ctx, parts[0])
        for part in parts[1:]:
            if resolved is None or not isinstance(resolved, click.Group):
                return None
            resolved = resolved.get_command(ctx, part)
        if resolved is not None:
            warn_once(cmd_name, target)
        return resolved

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        name, command, rest = super().resolve_command(ctx, args)
        # Report the name the user typed, so usage lines and errors echo their
        # own command back rather than a name they have never seen.
        return name, command, rest


class ModeGroup(click.Group):
    """A group whose subcommands replace a flag that switched modes.

    ``hle service install`` carried twenty flags across three mutually
    exclusive modes, with help text that had to prefix individual options with
    "fp mode:" — the code saying, in as many words, that it wanted to be three
    commands.

    It is three commands now. The flag form still works: anything that does not
    begin with a known subcommand is handed to the original command unchanged,
    so every unit file and install script written against it keeps working.
    """

    #: Name of the hidden command that accepts the old flag form.
    LEGACY = "_flags"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        first = args[0] if args else ""
        # `--help` belongs to the group: it is where the modes are listed.
        # An unknown bare word is reported against the group too, for the same
        # reason. Anything else that starts with a dash is the old flag form.
        if first.startswith("-") and first not in ("-h", "--help"):
            args = [self.LEGACY, *args]
        return super().parse_args(ctx, args)


class LegacyModeCommand(click.Command):
    """The old flag form, hidden behind a group without saying so.

    It is registered under an internal name so Click can dispatch to it, and
    that name has no business appearing in a usage line the user is meant to
    retype.
    """

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Click composes the usage line from ctx.command_path, so this is the
        # hook that covers both `--help` and the usage shown with an error.
        formatter.write_usage(
            ctx.command_path.replace(f" {ModeGroup.LEGACY}", ""),
            " ".join(self.collect_usage_pieces(ctx)),
        )


class RootGroup(AliasedGroup):
    """The top-level group: legacy names, plus global options anywhere."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        return super().parse_args(ctx, hoist_global_options(args))
