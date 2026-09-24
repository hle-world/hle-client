"""Pin the whole command tree, not just the pieces other tests happen to check.

The audit (docs/audits-2026-09-23/cli-audit.md §6) found no help-tree snapshot:
`webhook`/`preflight` went missing from `hle tunnel --help` — the same command
objects, registered a second time with `hidden=True` re-applied to the shared
object — and nothing failed, because no test walked the tree and looked at
every node. This one does: every command and group, hidden or not, every
param's name/type/required/multiple/envvar/whether a default is set, the first
line of help, and every alias table. A refactor that moves an option, renames
a flag, or drops a command out of `--help` shows up as a diff here instead of
as a support ticket.

On mismatch this prints a unified diff and tells you how to regenerate the
fixture — it does not silently do it for you, because a snapshot that updates
itself on failure isn't a lock.
"""

from __future__ import annotations

import difflib
import os

from hle_client.cli import main

from ._cli_introspect import render_tree

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "cli_tree.txt")


def _current() -> str:
    return render_tree(main)


def test_cli_tree_matches_fixture() -> None:
    current = _current()

    if os.environ.get("UPDATE_CLI_TREE"):
        with open(_FIXTURE, "w") as f:
            f.write(current)
        return

    with open(_FIXTURE) as f:
        expected = f.read()

    if current != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                current.splitlines(),
                fromfile="tests/unit/fixtures/cli_tree.txt (committed)",
                tofile="current CLI tree",
                lineterm="",
            )
        )
        raise AssertionError(
            "The CLI command tree changed — a command, option, hidden flag or help "
            "line was added, removed, or edited.\n\n"
            f"{diff}\n\n"
            "If this is intentional, regenerate the fixture and commit it:\n"
            "  UPDATE_CLI_TREE=1 uv run pytest tests/unit/test_cli_tree_snapshot.py"
        )
