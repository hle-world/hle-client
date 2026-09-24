"""The service layer: what the CLI, the TUI and the webapp all do, done once.

Every function here is ``async``, takes an ``ApiClient`` (or nothing, for
local state), returns a typed model from :mod:`hle_client.ops.models` and
fails by raising a typed error from :mod:`hle_client.errors`. Nothing here
prints, prompts, or imports click. A decision that needs a human — "the
tunnel is live, delete anyway?" — comes back as data (:class:`Conflict`) and
the caller decides what to do with it.

The layer exists because the same operation used to be written three times:
once in a CLI leaf, once in ``hle-tui``'s data module, once in the webapp's
proxy helpers. They drifted. ``hle status`` read ``is_online`` while the relay
sent ``online``, and every agent showed offline. With one mapper per model,
that class of bug has one place to be wrong and one test to catch it.
"""

from hle_client.ops import access, agents, auth, daemon, tunnels
from hle_client.ops.models import (
    AccessDiff,
    AccessRule,
    Agent,
    BasicAuthStatus,
    Conflict,
    Daemon,
    Deleted,
    PinStatus,
    ShareLink,
    Tunnel,
    TunnelDetail,
)

__all__ = [
    "AccessDiff",
    "AccessRule",
    "Agent",
    "BasicAuthStatus",
    "Conflict",
    "Daemon",
    "Deleted",
    "PinStatus",
    "ShareLink",
    "Tunnel",
    "TunnelDetail",
    "access",
    "agents",
    "auth",
    "daemon",
    "tunnels",
]
