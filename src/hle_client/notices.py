"""Render server-pushed NOTICE messages to the user's terminal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from hle_common.protocol import NoticePayload

_console = Console()

_GLYPHS = {
    "info": ("ℹ", "cyan"),
    "success": ("✓", "green"),
    "warning": ("⚠", "yellow"),
    "error": ("✗", "red"),
}


def render_notice(notice: NoticePayload) -> None:
    glyph, colour = _GLYPHS.get(notice.level, _GLYPHS["info"])
    line = f"[{colour}]{glyph}[/{colour}] {notice.message}"
    if notice.url:
        line += f" [dim]→ {notice.url}[/dim]"
    _console.print(line)
