"""Tests for the client-side NOTICE renderer."""

from __future__ import annotations

import io

import pytest

from hle_client import notices
from hle_client.richcompat import Console
from hle_common.protocol import NoticePayload


@pytest.fixture
def captured_console(monkeypatch):
    buf = io.StringIO()
    fake = Console(file=buf, force_terminal=False, width=200, no_color=True)
    monkeypatch.setattr(notices, "_console", fake)
    return buf


def test_renders_message(captured_console):
    notices.render_notice(NoticePayload(code="x", message="Hello world"))
    assert "Hello world" in captured_console.getvalue()


def test_renders_url(captured_console):
    notices.render_notice(
        NoticePayload(code="x", message="Click here", url="https://hle.world/dashboard")
    )
    out = captured_console.getvalue()
    assert "Click here" in out
    assert "https://hle.world/dashboard" in out


def test_each_level_has_glyph(captured_console):
    for lvl, glyph in [("info", "ℹ"), ("success", "✓"), ("warning", "⚠"), ("error", "✗")]:
        notices.render_notice(NoticePayload(level=lvl, code="x", message=lvl))
        assert glyph in captured_console.getvalue()


def test_unknown_level_falls_back_to_info(captured_console):
    # NoticePayload would reject this, but the renderer must be defensive in
    # case the server adds a new level the client does not yet know about.
    notice = NoticePayload(code="x", message="hi")
    object.__setattr__(notice, "level", "future-level")
    notices.render_notice(notice)
    assert "hi" in captured_console.getvalue()


def test_server_text_is_inert(captured_console):
    """A hostile relay cannot inject terminal escapes or a rich link.

    The message carries an OSC 52 clipboard write and a rich ``[link=...]``;
    both must reach the terminal as ordinary characters, not as instructions.
    """
    notices.render_notice(
        NoticePayload(
            code="x",
            message="\x1b]52;c;ZXZpbA==\x07 [link=https://evil]click[/link]",
        )
    )
    out = captured_console.getvalue()
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "[link=https://evil]click[/link]" in out


def test_url_is_inert_too(captured_console):
    """The URL is server-supplied as well, so it gets the same treatment."""
    notices.render_notice(
        NoticePayload(code="x", message="see", url="\x1b]0;spoof\x07[link=x]y[/link]")
    )
    out = captured_console.getvalue()
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "[link=x]y[/link]" in out
