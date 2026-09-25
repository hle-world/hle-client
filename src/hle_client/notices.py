"""What a running tunnel tells whoever is watching it: people, and supervisors.

People get server-pushed NOTICE messages rendered with a glyph. Supervisors —
hle-webapp, the Home Assistant add-on, hle-docker — used to recover the same
facts by scraping those glyphs out of stdout, which breaks the day a glyph or
a colour changes. ``--events jsonl`` gives them the facts instead: one JSON
object per line on stdout, and the human text moves to stderr so stdout
carries nothing else. The schema is documented in ``docs/events.md``.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TextIO

from hle_client.richcompat import Console

if TYPE_CHECKING:
    from hle_common.protocol import NoticePayload

_console = Console()
_err_console = Console(stderr=True)

_GLYPHS = {
    "info": ("ℹ", "cyan"),
    "success": ("✓", "green"),
    "warning": ("⚠", "yellow"),
    "error": ("✗", "red"),
}

#: Values ``--events`` accepts. Only one today; a choice so another format
#: (or a version suffix) can be added without breaking the flag.
EVENT_FORMATS = ("jsonl",)

#: Every event name emitted. Documented in docs/events.md; a supervisor should
#: ignore names it does not know, so adding one is not a breaking change.
EVENT_NAMES = ("connected", "registered", "disconnected", "notice", "error", "fatal")

Level = Literal["info", "success", "warning", "error"]
Source = Literal["tunnel", "agent"]

# Where events go, or None when they are off. The stream is resolved per write
# when not given explicitly, so a caller that redirects sys.stdout later (a
# test runner, an embedding process) still receives them.
_enabled: bool = False
_stream: TextIO | None = None


def enable_events(fmt: str = "jsonl", stream: TextIO | None = None) -> None:
    """Start emitting events. Human output moves to stderr from here on."""
    global _enabled, _stream
    if fmt not in EVENT_FORMATS:
        raise ValueError(f"unknown event format {fmt!r}")
    _enabled = True
    _stream = stream


def disable_events() -> None:
    global _enabled, _stream
    _enabled = False
    _stream = None


def events_enabled() -> bool:
    return _enabled


def human_console() -> Console:
    """The console for text meant for a person.

    stdout normally; stderr while events are on, because then stdout belongs
    to the machine reading it and one stray banner line is a parse error.
    """
    return _err_console if _enabled else _console


def emit_event(
    event: str,
    *,
    level: Level = "info",
    label: str | None = None,
    subdomain: str | None = None,
    public_url: str | None = None,
    message: str = "",
    code: str | int | None = None,
    source: Source = "tunnel",
) -> None:
    """Write one event line, or nothing when events are off.

    ``code`` is always a string on the wire (a relay close code arrives as an
    int, a notice code as a string); None stays null.
    """
    if not _enabled:
        return
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": event,
        "level": level,
        "source": source,
        "label": label,
        "subdomain": subdomain,
        "public_url": public_url,
        "message": message,
        "code": None if code is None else str(code),
    }
    stream = _stream if _stream is not None else sys.stdout
    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def render_notice(
    notice: NoticePayload,
    *,
    label: str | None = None,
    subdomain: str | None = None,
    public_url: str | None = None,
) -> None:
    """Show a server NOTICE to the person, and to the supervisor as an event."""
    level: Level = notice.level if notice.level in _GLYPHS else "info"
    emit_event(
        "notice",
        level=level,
        label=label,
        subdomain=subdomain,
        public_url=public_url,
        message=notice.message,
        code=notice.code,
    )
    glyph, colour = _GLYPHS.get(notice.level, _GLYPHS["info"])
    line = f"[{colour}]{glyph}[/{colour}] {notice.message}"
    if notice.url:
        line += f" [dim]→ {notice.url}[/dim]"
    (_err_console if _enabled else _console).print(line)
