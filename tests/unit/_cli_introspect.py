"""Shared introspection helpers for the CLI-grammar-locking tests.

Not a test module itself (no ``test_`` prefix, so pytest does not collect it).
Walks the real ``click`` command tree built in ``hle_client.cli`` — including
hidden commands and each ``AliasedGroup``'s alias table — and produces a
deterministic textual description of it, plus a flat list of every leaf
command's full path. Three test files share this so the tree is walked the
same way everywhere: a snapshot test, a conventions test, and an argv-contract
test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import click


@dataclass(frozen=True)
class ParamInfo:
    kind: str  # "argument" | "option"
    opts: tuple[str, ...]
    name: str
    type_name: str
    required: bool
    multiple: bool
    nargs: int
    is_flag: bool
    envvar: str | None
    default_is_set: bool
    hidden: bool

    def render(self) -> str:
        return (
            f"    {self.kind:8} {'/'.join(self.opts):28} name={self.name} "
            f"type={self.type_name} required={self.required} multiple={self.multiple} "
            f"nargs={self.nargs} flag={self.is_flag} envvar={self.envvar} "
            f"default_set={self.default_is_set} hidden={self.hidden}"
        )


@dataclass(frozen=True)
class CommandNode:
    path: str  # e.g. "hle tunnel create"
    kind: str  # "group" | "command"
    hidden: bool
    help_line: str
    params: tuple[ParamInfo, ...] = field(default_factory=tuple)

    def render(self) -> str:
        lines = [f"{self.path}  [{self.kind}]  hidden={self.hidden}  help={self.help_line!r}"]
        lines.extend(p.render() for p in self.params)
        return "\n".join(lines)


def _param_info(p: click.Parameter) -> ParamInfo:
    envvar: str | None = (
        p.envvar if p.envvar is None or isinstance(p.envvar, str) else ",".join(p.envvar)
    )
    return ParamInfo(
        kind="argument" if isinstance(p, click.Argument) else "option",
        opts=tuple(p.opts),
        name=p.name or "",
        type_name=getattr(p.type, "name", type(p.type).__name__),
        required=bool(p.required),
        multiple=bool(getattr(p, "multiple", False)),
        nargs=p.nargs,
        is_flag=bool(getattr(p, "is_flag", False)),
        envvar=envvar,
        default_is_set=p.default is not None,
        hidden=bool(getattr(p, "hidden", False)),
    )


def _help_first_line(cmd: click.Command) -> str:
    text = cmd.help or cmd.short_help or ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def walk(root: click.Group, *, root_name: str = "hle") -> list[CommandNode]:
    """Every command reachable from ``root``, hidden ones included.

    Returns nodes sorted by path, so the result does not depend on the order
    commands happen to be registered in — only on what exists.
    """
    nodes: list[CommandNode] = []

    def _visit(cmd: click.Command, path: str) -> None:
        ctx = click.Context(cmd, info_name=path)
        params = tuple(_param_info(p) for p in cmd.get_params(ctx))
        kind = "group" if isinstance(cmd, click.Group) else "command"
        nodes.append(
            CommandNode(
                path=path,
                kind=kind,
                hidden=bool(cmd.hidden),
                help_line=_help_first_line(cmd),
                params=params,
            )
        )
        if isinstance(cmd, click.Group):
            for name, child in cmd.commands.items():
                _visit(child, f"{path} {name}")

    _visit(root, root_name)
    nodes.sort(key=lambda n: n.path)
    return nodes


def alias_lines(root: click.Group, *, root_name: str = "hle") -> list[str]:
    """One line per alias entry, for every ``AliasedGroup`` in the tree.

    Aliases resolve dynamically in ``AliasedGroup.get_command`` rather than
    being registered as ordinary commands, so ``walk`` above never sees them —
    they have to be read off the ``.aliases`` attribute directly.
    """
    lines: list[str] = []

    def _visit(cmd: click.Command, path: str) -> None:
        aliases = getattr(cmd, "aliases", None)
        # Quiet aliases (old verbs) are marked; the rest print a stderr note.
        silent = getattr(cmd, "silent", frozenset())
        if aliases:
            for old, new in sorted(aliases.items()):
                mark = "  hidden" if old in silent else ""
                lines.append(f"{path} : {old} -> {new}{mark}")
        if isinstance(cmd, click.Group):
            for name, child in cmd.commands.items():
                _visit(child, f"{path} {name}")

    _visit(root, root_name)
    lines.sort()
    return lines


def render_tree(root: click.Group, *, root_name: str = "hle") -> str:
    nodes = walk(root, root_name=root_name)
    aliases = alias_lines(root, root_name=root_name)
    parts = [n.render() for n in nodes]
    if aliases:
        parts.append("ALIASES")
        parts.extend(f"  {line}" for line in aliases)
    return "\n".join(parts) + "\n"


def leaf_paths(root: click.Group, *, root_name: str = "hle") -> list[str]:
    """The path of every non-group command, visible or hidden."""
    return [n.path for n in walk(root, root_name=root_name) if n.kind == "command"]
