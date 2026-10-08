"""The shape of the credentials the relay issues to agents.

The relay mints these and the client checks their shape before saving them,
so both sides import this one definition rather than each keeping a copy.
Only the *shape* is defined here; whether a credential is valid, and whose it
is, is always the relay's call.

* Current: an ordinary API key, ``hle_`` plus 32 lowercase hex characters
  (agents created since server v2609.2).
* Legacy: an ``hlea_``-prefixed agent token, still accepted by the relay.
"""

from __future__ import annotations

import re

__all__ = [
    "API_KEY_PATTERN",
    "LEGACY_AGENT_TOKEN_PREFIX",
    "is_agent_credential",
]

# The relay checks this exact length before it will even hash a key, so a
# value with a stray quote or a trailing newline is refused there.
API_KEY_PATTERN = re.compile(r"^hle_[0-9a-f]{32}$")

# Legacy agent enrollment tokens, issued before server v2609.2.
LEGACY_AGENT_TOKEN_PREFIX = "hlea_"


def is_agent_credential(token: str) -> bool:
    """Return True if ``token`` has the shape of an agent credential.

    A well-formed ``hle_`` key, or any ``hlea_``-prefixed token. Nothing is
    stripped: callers trim whitespace first, so a padded value is rejected.
    """
    return API_KEY_PATTERN.fullmatch(token) is not None or token.startswith(
        LEGACY_AGENT_TOKEN_PREFIX
    )
