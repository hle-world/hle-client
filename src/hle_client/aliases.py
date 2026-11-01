"""Old spellings keep working, with a deprecation warning, to be removed later.

The command names changed to one grammar — ``hle <noun> <verb>``. Scripts,
systemd units, blog posts and support answers written against the old names
did not change with them, and a CLI that breaks those has not improved for the
person it broke.

So the old names still resolve, but every one of them prints a single line to
stderr naming the exact replacement::

    Deprecated: 'hle <old>' will be removed in a future release; use 'hle <new>'.

They are hidden from ``--help``, so the shape being taught is the new one. It
goes to stderr because a script parsing stdout must not start seeing advice
mixed into its data. ``HLE_NO_DEPRECATION_WARNINGS`` suppresses it — every
generated service unit sets it, so an old unit's log does not fill with
warnings on every start. A short spelling worth keeping is not a legacy one,
though, so a group can also advertise a visible alias beside its command;
those never warn.

The old spellings are removed in a later release, once the installer's version
floor and the service-unit refresh have moved everyone off them.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any, TypeVar

import click

from hle_client.errors import HleError
from hle_client.output import from_ctx

F = TypeVar("F", bound=Callable[..., Any])

# Printed once per process. A loop calling `hle config list` in a script would
# otherwise emit the same nag on every iteration.
_warned: set[str] = set()

_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _deprecation_silenced() -> bool:
    """Whether deprecation warnings are off for this process.

    ``HLE_NO_DEPRECATION_WARNINGS=1`` is set by every generated service unit,
    plist and rc script (``service_cmd._QUIET_ENV``), so a unit running an old
    spelling does not put a warning in its log on every start.
    """
    value = os.environ.get("HLE_NO_DEPRECATION_WARNINGS")
    if value is None:
        return False
    return value.strip().lower() not in _FALSEY


def _relative_command_path(ctx: click.Context, *parts: str) -> str:
    """A command path without the leading program name, for a warning.

    ``ctx.command_path`` starts with the program name (``hle`` on the command
    line, but the command object's own name under ``CliRunner``), which the
    warning spells itself; only the command path below it matters.
    """
    tokens = ctx.command_path.split(" ")
    path = " ".join(tokens[1:])
    return " ".join(piece for piece in (path, *parts) if piece)


def warn_deprecated(old: str, new: str) -> None:
    """Tell the user the exact replacement for an old spelling, once.

    ``old`` and ``new`` are command spellings without the leading ``hle``.
    """
    if old in _warned or _deprecation_silenced():
        return
    _warned.add(old)
    print(
        f"Deprecated: 'hle {old}' will be removed in a future release; use 'hle {new}'.",
        file=sys.stderr,
    )


def deprecated_option(*param_decls: str, replacement: str, **attrs: Any) -> Callable[[F], F]:
    """A hidden option kept for an old spelling, warning when it is used.

    ``replacement`` is the current option (e.g. ``--auth``) or the usage that
    replaced it. The warning names the command and the exact replacement, and
    is emitted only when the option was actually given on the command line.
    """
    from click.core import ParameterSource

    attrs["hidden"] = True

    def _warn_callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if not ctx.resilient_parsing and (
            ctx.get_parameter_source(param.name) == ParameterSource.COMMANDLINE
        ):
            warn_deprecated(
                _relative_command_path(ctx, param.opts[0]),
                _relative_command_path(ctx, replacement),
            )
        return value

    def _decorate(f: F) -> F:
        return click.option(*param_decls, callback=_warn_callback, **attrs)(f)

    return _decorate


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

    Everything after ``--`` is left exactly as it is: ``hle firepuncher box
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

    Every old spelling warns once, naming the replacement: the renamed nouns
    (``aliases``), the old verbs inside a noun (``hidden_aliases``) and the
    hidden commands kept whole (``deprecated_commands``, e.g. ``hle expose``).
    ``visible_aliases`` are short spellings that stay advertised (``fp``) and
    are not deprecations, so they never warn. All are in ``self.aliases`` so
    every walker of the tree sees every spelling; the warning itself is raised
    in ``resolve_command``, which only runs when a user actually typed one, not
    when ``--help`` formats the table.
    """

    def __init__(
        self,
        *args: Any,
        aliases: dict[str, str] | None = None,
        hidden_aliases: dict[str, str] | None = None,
        visible_aliases: dict[str, str] | None = None,
        deprecated_commands: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.aliases: dict[str, str] = {
            **(aliases or {}),
            **(visible_aliases or {}),
            **(hidden_aliases or {}),
        }
        self.visible_aliases: frozenset[str] = frozenset(visible_aliases or ())
        self.hidden_alias_names: frozenset[str] = frozenset(hidden_aliases or ())
        # Hidden commands kept whole (``expose``) cannot go in the alias table,
        # because their options differ from the new command. They are mapped to
        # their replacement here so the warning still names it.
        self.deprecated_commands: dict[str, str] = dict(deprecated_commands or {})

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
        return resolved

    def _warn_if_deprecated(self, ctx: click.Context, name: str) -> None:
        """Warn when *name* is an old spelling the user actually typed."""
        if ctx.resilient_parsing or name in self.visible_aliases:
            return
        replacement = self.aliases.get(name) or self.deprecated_commands.get(name)
        if replacement is None:
            return
        warn_deprecated(
            _relative_command_path(ctx, name),
            _relative_command_path(ctx, replacement),
        )

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        name, command, rest = super().resolve_command(ctx, args)
        # Report the name the user typed, so usage lines and errors echo their
        # own command back rather than a name they have never seen.
        if name is not None:
            self._warn_if_deprecated(ctx, name)
        return name, command, rest

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """List the commands, with any advertised short alias beside its name.

        The alias resolves in ``get_command`` rather than being registered as a
        second command, so it never shows up as a row of its own — the group
        would list the same command twice and drift the moment one moved. Here
        it is attached to the command it names instead: ``firepuncher (fp)``.
        """
        commands: list[tuple[str, click.Command]] = []
        for subcommand in self.list_commands(ctx):
            command = self.get_command(ctx, subcommand)
            if command is None or command.hidden:
                continue
            commands.append((subcommand, command))
        if not commands:
            return

        advertised: dict[str, list[str]] = {}
        for alias in sorted(self.visible_aliases):
            head = self.aliases[alias].split()[0]
            advertised.setdefault(head, []).append(alias)

        width = formatter.width or 80
        limit = width - 6 - max(len(name) for name, _ in commands)
        rows: list[tuple[str, str]] = []
        for subcommand, command in commands:
            aliases = advertised.get(subcommand)
            label = f"{subcommand} ({', '.join(aliases)})" if aliases else subcommand
            rows.append((label, command.get_short_help_str(limit)))
        with formatter.section("Commands"):
            formatter.write_dl(rows)


class ModeGroup(AliasedGroup):
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

    def __init__(
        self,
        *args: Any,
        deprecated_modes: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # flag -> the subcommand (with its own usage) that replaced it, so the
        # warning can name the exact replacement.
        self.deprecated_modes: dict[str, str] = dict(deprecated_modes or {})

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        first = args[0] if args else ""
        # `--help` belongs to the group: it is where the modes are listed.
        # An unknown bare word is reported against the group too, for the same
        # reason. Anything else that starts with a dash is the old flag form.
        if first.startswith("-") and first not in ("-h", "--help"):
            self._warn_legacy_mode(ctx, args)
            args = [self.LEGACY, *args]
        return super().parse_args(ctx, args)

    def _warn_legacy_mode(self, ctx: click.Context, args: list[str]) -> None:
        """Warn once about the flag form, naming the mode it selected."""
        if ctx.resilient_parsing:
            return
        flag = next((a for a in args if a in self.deprecated_modes), "")
        replacement = self.deprecated_modes.get(flag, "")
        if not replacement:
            return
        warn_deprecated(
            _relative_command_path(ctx, flag),
            _relative_command_path(ctx, replacement),
        )


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
    """The top-level group: legacy names, global options anywhere, one error path."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        return super().parse_args(ctx, hoist_global_options(args))

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except HleError as exc:
            # The only place an error is rendered. Commands raise; this
            # decides what that looks like — stderr text, or JSON under
            # `-o json` — and what the process exits with.
            from_ctx(ctx).report(exc.message, hint=exc.hint, payload=exc.as_dict())
            ctx.exit(exc.exit_code)
