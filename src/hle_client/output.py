"""One place that decides how output looks.

Three things were tangled together before: every command built its own
``rich.Console``, every command decided for itself whether to offer machine
output (three of about twenty-five did), and colour was unconditional — no
``NO_COLOR``, no check for whether a terminal was even attached.

They are one decision, made once here:

* **rich is optional.** It is the only dependency a small device does not need
  — a pfSense or OpenWrt box wants the tunnel, not tables. Without it, output
  degrades to plain text through the same calls, so nothing needs a second
  code path.
* **Colour follows the environment**: ``NO_COLOR``, ``--no-color``, or output
  that is not a terminal all mean plain text. A pipe gets clean bytes.
* **Machine output is a global flag**, not a per-command afterthought. Any
  command that renders a resource can answer ``-o json``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

try:  # pragma: no cover - exercised by both installs, not by one test run
    from rich.console import Console as _RichConsole
    from rich.table import Table as _RichTable

    HAVE_RICH = True
except ImportError:  # pragma: no cover
    _RichConsole = None  # type: ignore[assignment,misc]
    _RichTable = None  # type: ignore[assignment,misc]
    HAVE_RICH = False

# Style tags this understands, so stripping them from plain output cannot eat
# real text. A bare regex for "[...]" would swallow "[1]" out of a log line or
# a bracketed IPv6 address, which is worse than leaving a tag in.
_STYLES = (
    "bold|dim|italic|underline|reverse|strike|"
    "red|green|yellow|blue|magenta|cyan|white|black|"
    "bright_red|bright_green|bright_yellow|bright_blue|bright_magenta|bright_cyan|"
    "bright_white|bright_black|grey|gray"
)
_TAG = re.compile(rf"\[/?(?:{_STYLES})(?:\s+(?:{_STYLES}))*\]|\[/\]")

TABLE = "table"
JSON = "json"
OUTPUT_FORMATS = (TABLE, JSON)


def strip_markup(text: str) -> str:
    """Drop style tags, leaving the words."""
    return _TAG.sub("", text)


def color_enabled(explicit: bool | None = None, stream: Any = None) -> bool:
    """Whether to emit colour at all.

    ``NO_COLOR`` is honoured as an environment convention (https://no-color.org):
    set to anything, colour is off. An explicit ``--no-color`` wins over the
    environment, and a stream that is not a terminal is never coloured.
    """
    if explicit is not None:
        return explicit
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("HLE_NO_COLOR"):
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def from_ctx(ctx: Any) -> Output:
    """The invocation's Output, or a default one.

    A command can be invoked without the root group having run — in tests, and
    through ``ctx.invoke`` — so this never assumes ``ctx.obj`` was populated.
    """
    obj = getattr(ctx, "obj", None)
    if isinstance(obj, dict):
        existing = obj.get("output")
        if isinstance(existing, Output):
            return existing
    return Output()


def api_key_from_ctx(ctx: Any, explicit: str | None) -> str | None:
    """Resolve the API key: the command's own flag first, then the root's.

    Precedence is flag → env → config file, and the root group already folded
    env into its own value, so a leaf that was given nothing inherits it.
    """
    if explicit:
        return explicit
    obj = getattr(ctx, "obj", None)
    if isinstance(obj, dict):
        inherited = obj.get("api_key")
        if isinstance(inherited, str) and inherited:
            return inherited
    return None


class Output:
    """Rendering for one CLI invocation.

    Held on the Click context so a subcommand inherits the root's decision
    rather than making its own.
    """

    def __init__(
        self,
        *,
        fmt: str = TABLE,
        color: bool | None = None,
        quiet: bool = False,
        stderr: bool = False,
    ) -> None:
        self.fmt = fmt
        self.quiet = quiet
        self._stderr = stderr
        self._color = color_enabled(color, sys.stderr if stderr else sys.stdout)
        self._console = (
            _RichConsole(no_color=not self._color, stderr=stderr, soft_wrap=False)
            if HAVE_RICH
            else None
        )

    @property
    def _stream(self) -> Any:
        # Resolved per write. Binding it in __init__ meant anything that
        # redirected stdout afterwards — a test runner, a caller capturing
        # output — got nothing, while the text went to the original handle.
        return sys.stderr if self._stderr else sys.stdout

    @property
    def json_mode(self) -> bool:
        return self.fmt == JSON

    def print(self, text: str = "", *, force: bool = False) -> None:
        """Write a line of human-facing text.

        Suppressed by ``--quiet`` and by ``-o json``, either of which means the
        caller wants only the payload. ``force`` is for text that is the answer
        itself rather than commentary.
        """
        if not force and (self.quiet or self.json_mode):
            return
        if self._console is not None:
            self._console.print(text)
        else:
            print(strip_markup(text), file=self._stream)

    def error(self, text: str) -> None:
        """Write to stderr, regardless of quiet or output format.

        Errors are never suppressed: a silent failure is the thing ``--quiet``
        users complain about, and in JSON mode stdout must stay parseable, so
        the message cannot go there.
        """
        if HAVE_RICH and _RichConsole is not None:
            _RichConsole(stderr=True, no_color=not self._color).print(text)
        else:
            print(strip_markup(text), file=sys.stderr)

    def data(self, payload: Any) -> None:
        """Emit a resource. JSON when asked for, otherwise nothing.

        Commands render their own human view; this exists so every one of them
        can answer ``-o json`` without deciding how.
        """
        if self.json_mode:
            print(json.dumps(payload, indent=2, default=str), file=self._stream)

    def table(
        self,
        columns: list[str],
        rows: list[list[str]],
        *,
        title: str | None = None,
    ) -> None:
        """Render rows, as a table with rich and as aligned columns without it."""
        if self.quiet or self.json_mode:
            return
        if self._console is not None and _RichTable is not None:
            table = _RichTable(title=title, show_header=True, header_style="bold")
            for column in columns:
                table.add_column(column)
            for row in rows:
                table.add_row(*row)
            self._console.print(table)
            return

        plain_rows = [[strip_markup(cell) for cell in row] for row in rows]
        widths = [len(c) for c in columns]
        for row in plain_rows:
            for i, cell in enumerate(row[: len(widths)]):
                widths[i] = max(widths[i], len(cell))
        if title:
            print(title, file=self._stream)
        header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
        print(header, file=self._stream)
        print("  ".join("-" * w for w in widths), file=self._stream)
        for row in plain_rows:
            print(
                "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[: len(widths)])),
                file=self._stream,
            )
