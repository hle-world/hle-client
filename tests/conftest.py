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
    "HLE_DISCOVERY_EXCLUDE_NAMESPACES",
)


@pytest.fixture(autouse=True)
def _not_in_a_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("KUBERNETES_"):
            monkeypatch.delenv(name, raising=False)
    for name in _CLUSTER_ENV:
        monkeypatch.delenv(name, raising=False)
