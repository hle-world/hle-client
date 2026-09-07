"""``Console`` and ``Table`` that work whether or not rich is installed.

rich is the one dependency a constrained device does not need. A pfSense or
OpenWrt box wants the tunnel; it does not want a terminal rendering library to
get one. So ``rich`` moved to the ``pretty`` extra, and this module is what
lets the rest of the code go on calling ``console.print("[green]ok[/green]")``
without caring which install it is running under.

The fallbacks are deliberately small. They cover exactly what this codebase
uses — printing marked-up strings and rendering simple tables — and nothing
else. When rich is present it is used unchanged, so the pretty install behaves
exactly as it always did.
"""

from __future__ import annotations

import sys
from typing import Any

from hle_client.output import strip_markup

try:  # pragma: no cover - both branches run, but never in one process
    from rich.console import Console
    from rich.table import Table

    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False

    class Table:  # type: ignore[no-redef]
        """Collects rows and prints them as aligned columns.

        Accepts (and ignores) rich's styling keywords so call sites do not need
        to know which implementation they got.
        """

        def __init__(
            self,
            title: str | None = None,
            *,
            show_header: bool = True,
            box: Any = None,
            padding: Any = None,
            header_style: str | None = None,
            **_: Any,
        ) -> None:
            self.title = title
            self.show_header = show_header
            self.columns: list[str] = []
            self.rows: list[list[str]] = []

        def add_column(self, header: str = "", **_: Any) -> None:
            self.columns.append(header)

        def add_row(self, *cells: Any, **_: Any) -> None:
            self.rows.append([strip_markup(str(c)) for c in cells])

        def render(self) -> str:
            width_count = max([len(self.columns), *(len(r) for r in self.rows)])
            headers = list(self.columns) + [""] * (width_count - len(self.columns))
            widths = [len(h) for h in headers]
            for row in self.rows:
                for i, cell in enumerate(row[:width_count]):
                    # A cell can span lines (access rules are joined with \n).
                    widest = max((len(line) for line in cell.split("\n")), default=0)
                    widths[i] = max(widths[i], widest)

            lines: list[str] = []
            if self.title:
                lines.append(self.title)
            if self.show_header and any(headers):
                lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
                lines.append("  ".join("-" * w for w in widths).rstrip())
            for row in self.rows:
                padded = list(row) + [""] * (width_count - len(row))
                lines.append(
                    "  ".join(
                        cell.replace("\n", " ").ljust(widths[i]) for i, cell in enumerate(padded)
                    ).rstrip()
                )
            return "\n".join(lines)

    class Console:  # type: ignore[no-redef]
        """Just enough of rich's Console to print text and tables."""

        def __init__(self, *, stderr: bool = False, file: Any = None, **_: Any) -> None:
            self._stderr = stderr
            self._file = file

        @property
        def _stream(self) -> Any:
            # An explicit file wins; otherwise resolved per write, never
            # captured in __init__. A console built at import time would
            # otherwise hold the original stdout, and anything that redirects
            # it later — a test runner, a caller capturing output — would see
            # nothing at all.
            if self._file is not None:
                return self._file
            return sys.stderr if self._stderr else sys.stdout

        def print(self, *objects: Any, **_: Any) -> None:
            if not objects:
                print(file=self._stream)
                return
            for obj in objects:
                if isinstance(obj, Table):
                    # `Table` here is the fallback defined just above; mypy
                    # binds the name to rich's Table from the `try` branch,
                    # which is the one branch this code cannot be reached from.
                    print(obj.render(), file=self._stream)  # type: ignore[attr-defined]
                else:
                    print(strip_markup(str(obj)), file=self._stream)


__all__ = ["HAVE_RICH", "Console", "Table"]
