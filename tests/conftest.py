"""Suite-wide test isolation.

CI runs inside a Kubernetes pod, where ``KUBERNETES_SERVICE_HOST`` (and the
``KUBERNETES_*`` service variables) are always set. The agent treats that as
"running in a cluster" and turns on the endpoint target guard, which would make
every ordinary agent test behave like a cluster agent (and resolve DNS). Tests
that are about cluster behaviour set these variables themselves.
"""

from __future__ import annotations

import os

import pytest

_CLUSTER_ENV = (
    "HLE_INSTALL_METHOD",
    "HLE_ALLOW_RAW_URLS",
    "HLE_NODE_IP",
    "HLE_POD_NAMESPACE",
    "HLE_CLUSTER_DOMAIN",
    "HLE_FIREPUNCHER_ENABLED",
    "HLE_FIREPUNCHER_ALLOW_METADATA",
    "HLE_DISCOVERY_EXCLUDE_NAMESPACES",
    # Self-update policy, so a developer's own agent.toml / environment cannot
    # change what the update tests see.
    "HLE_REMOTE_UPDATE",
    "HLE_ALLOW_DOWNGRADE",
    "HLE_UPDATE_MANIFEST",
)


@pytest.fixture(autouse=True)
def _isolated_agent_config(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Keep the suite off the developer's real ``~/.config/hle/agent.toml``.

    The self-update policy reads that file, so a ``remote_update = false`` in a
    local checkout would silently change the update tests. Tests that exercise
    the file set ``HLE_AGENT_CONFIG`` (or patch ``AGENT_CONFIG_PATH``)
    themselves, overriding this.
    """
    from hle_client import config

    path = tmp_path_factory.mktemp("agent-config") / "agent.toml"
    monkeypatch.delenv("HLE_AGENT_CONFIG", raising=False)
    monkeypatch.setattr(config, "AGENT_CONFIG_PATH", path)


@pytest.fixture(autouse=True)
def _not_in_a_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("KUBERNETES_"):
            monkeypatch.delenv(name, raising=False)
    for name in _CLUSTER_ENV:
        monkeypatch.delenv(name, raising=False)
