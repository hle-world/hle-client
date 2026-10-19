"""Answer ``logs_request`` from the agent's own in-memory log ring buffer.

Deliberately narrow: this reads only the records this process logged (the ring
buffer in :mod:`hle_client.agent_update`), never a file, never another
process, and executes nothing. Lines are scrubbed of credentials before they
leave the machine and clamped to the protocol limits.
"""

from __future__ import annotations

import re
from typing import Any

from hle_client import agent_update
from hle_common.agent_protocol import (
    LOGS_CAPABILITY,
    MAX_LOG_LINE_CHARS,
    LogsRequest,
    LogsResponse,
)

__all__ = ["LOGS_CAPABILITY", "build_response", "redact"]

_MASK = "[redacted]"

# Order matters: the Authorization/Cookie rule eats the rest of the line first,
# so a "Basic ..." or "Bearer ..." value is never left half-visible; the
# userinfo rule runs last so it sees whatever the earlier rules left.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Credential-bearing headers: the whole value, to the end of the line.
    (
        re.compile(r"(?i)\b((?:proxy-)?authorization|(?:set-)?cookie|x-api-key)[\"']?\s*[:=]\s*.*"),
        rf"\1: {_MASK}",
    ),
    (re.compile(r"(?i)\b((?:bearer|basic)\s+)[^\s\"',;]+"), rf"\1{_MASK}"),
    # Real key shapes, any length (see config.API_KEY_PATTERN /
    # AGENT_TOKEN_PREFIX), so module and logger names such as
    # ``hle_client.agent`` are left alone: they are not followed by a key
    # alphabet, so the trailing word boundary never matches.
    (re.compile(r"\bhle_[0-9a-f]+\b"), _MASK),
    (re.compile(r"\bhlea_[A-Za-z0-9_-]+\b"), _MASK),
    # ``password=x``, ``?access_token=x``, ``vncticket: x``, ``"api_key": "x"``.
    (
        re.compile(
            r"(?i)(\b[\w-]*(?:password|passwd|token|secret|api[_-]?key|ticket|auth|sig)"
            r"[\w-]*[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;]+"
        ),
        rf"\1{_MASK}",
    ),
    # URL userinfo: scheme://user:pass@host -> scheme://[redacted]@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@"), rf"\1{_MASK}@"),
]


def redact(line: str) -> str:
    """Mask token-looking strings and credential values in one log line."""
    for pattern, repl in _PATTERNS:
        line = pattern.sub(repl, line)
    return line


def build_response(msg: dict[str, Any]) -> LogsResponse:
    """Build the ``logs_response`` for a raw ``logs_request`` frame."""
    req = LogsRequest.model_validate(msg)
    available = list(agent_update.ensure_log_buffer().lines)
    tail = available[-req.lines :]
    return LogsResponse(
        request_id=req.request_id,
        lines=[redact(x)[:MAX_LOG_LINE_CHARS] for x in tail],
        truncated=len(available) > len(tail),
    )
