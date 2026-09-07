"""Reconcile the credential environment older installs were set up with.

HLE issued two credential shapes: ``hle_`` API keys and ``hlea_`` agent
tokens. Agents now enrol with a scoped ``hle_`` key instead, so there is one
shape to explain and one variable to set. Installs from before that carry an
agent token in ``HLE_API_KEY`` — the variable their unit file has always set,
and the one the relay still accepts for registering tunnels.

Nothing about that is broken, but the token is invisible to everything that
reads ``HLE_AGENT_TOKEN``, which is where an agent token now belongs. Copying
it across is the whole fix.

``HLE_API_KEY`` is deliberately left in place. The relay accepts an agent
token there for tunnel registration, so clearing it would break the very
installs this exists to help.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import MutableMapping

AGENT_TOKEN_PREFIX = "hlea_"

NOTICE = (
    "HLE_API_KEY holds a legacy agent token; reading it as HLE_AGENT_TOKEN as well. "
    "Re-enrol this machine to move to a scoped API key."
)


def normalize_credential_env(env: MutableMapping[str, str] | None = None) -> str | None:
    """Mirror a legacy agent token from ``HLE_API_KEY`` into ``HLE_AGENT_TOKEN``.

    Returns a one-line notice when something changed, otherwise ``None``. An
    ``HLE_AGENT_TOKEN`` that is already set always wins: it is the newer of
    the two settings and the operator set it deliberately.
    """
    target = os.environ if env is None else env
    api_key = (target.get("HLE_API_KEY") or "").strip()
    if not api_key.startswith(AGENT_TOKEN_PREFIX):
        return None
    if (target.get("HLE_AGENT_TOKEN") or "").strip():
        return None
    target["HLE_AGENT_TOKEN"] = api_key
    return NOTICE
