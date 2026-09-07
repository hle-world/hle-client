"""Who this client process is, as distinct from what it is asking for.

The relay needs to tell "the same client reconnecting" from "a second copy of
the client on another machine". Those look identical in a registration — both
are simply a request for a label the account owns — so the second silently
evicted the first, and where both kept retrying the two took the tunnel off
each other about once a second, indefinitely.

An identity per *process* is what separates them. It is deliberately not
persisted: a restarted client is a new process and may legitimately take over
from the connection its predecessor left behind, while a copy running
concurrently on another host must not.
"""

from __future__ import annotations

import os
import socket
import uuid

# Generated once per process, at import. Never written to disk: persisting it
# would make a restart indistinguishable from a duplicate, which is the very
# distinction this exists to draw.
_INSTANCE_ID = uuid.uuid4().hex


def instance_id() -> str:
    """A stable, unique id for this client process."""
    return _INSTANCE_ID


def hostname() -> str | None:
    """Best-effort machine name, for telling the owner *where* a duplicate runs.

    Shown only back to the account that owns the agent, and only to answer
    "which of my machines is the other one". ``HLE_HOSTNAME`` overrides it for
    hosts whose real name is meaningless (a container id, say), and a failure
    to resolve one is not worth an error — the id above is what actually
    distinguishes the processes.
    """
    override = os.environ.get("HLE_HOSTNAME", "").strip()
    if override:
        return override[:128]
    try:
        name = socket.gethostname().strip()
    except OSError:
        return None
    return name[:128] or None
