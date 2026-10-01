"""Namespace exclusions shared by service discovery and the target allow-list.

``HLE_DISCOVERY_EXCLUDE_NAMESPACES`` (comma-separated names) is added to the
built-in skips, so an excluded namespace is both hidden from discovery and
refused as an endpoint target by :mod:`hle_client.k8s_targets`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

EXCLUDE_NAMESPACES_ENV = "HLE_DISCOVERY_EXCLUDE_NAMESPACES"

# Cluster infrastructure that is never interesting to expose, and in
# kube-system's case actively dangerous to put on the internet.
DEFAULT_EXCLUDE_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})


def excluded_namespaces(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The built-in skips plus anything named in the environment (lowercased)."""
    raw = (os.environ if env is None else env).get(EXCLUDE_NAMESPACES_ENV) or ""
    extra = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return DEFAULT_EXCLUDE_NAMESPACES | extra
