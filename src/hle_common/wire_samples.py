"""Representative instances of every wire model, for compatibility checking.

The protocol is a contract with every deployed client and with the server, so a
change to the serialisation layer has to be proved byte-identical rather than
assumed. These samples are snapshotted by ``scripts/capture_wire_baseline.py``
and asserted against in ``tests/unit/test_wire_compat.py``.

Each sample sets optional fields as well as required ones, because the bug this
guards against — a field silently appearing, disappearing, or changing shape —
only shows up when the optional fields are populated.
"""

from __future__ import annotations

from hle_common.agent_protocol import (
    AgentHello,
    AgentStateSync,
    AgentStatus,
    AgentWelcome,
    EndpointSpec,
    EndpointStatus,
)
from hle_common.discovery import DiscoveredService, DiscoveryRefresh, DiscoveryReport
from hle_common.fp_protocol import (
    ForwardRule,
    FpClose,
    FpData,
    FpError,
    FpHello,
    FpOpen,
    FpReady,
    FpWelcome,
)
from hle_common.models import (
    DiagnosticEvent,
    HttpResponseChunk,
    HttpResponseEnd,
    HttpResponseStart,
    LogConfig,
    ProxiedHttpRequest,
    ProxiedHttpResponse,
    SpeedTestData,
    SpeedTestResult,
    TunnelRegistration,
    TunnelRegistrationResponse,
    WsStreamAccept,
    WsStreamClose,
    WsStreamFrame,
    WsStreamOpen,
)
from hle_common.preflight import (
    PreflightFinding,
    PreflightFix,
    PreflightReport,
    PreflightRequest,
    Severity,
)
from hle_common.protocol import ErrorPayload, MessageType, NoticePayload, ProtocolMessage

_RULE = ForwardRule(host="localhost", port=22)

SAMPLES: dict[str, object] = {
    # -- protocol.py ------------------------------------------------------
    "ProtocolMessage.minimal": ProtocolMessage(type=MessageType.PING),
    "ProtocolMessage.full": ProtocolMessage(
        type=MessageType.HTTP_REQUEST,
        tunnel_id="tun-1",
        request_id="req-1",
        payload={"nested": {"a": 1}, "list": [1, 2], "null": None},
    ),
    "ErrorPayload": ErrorPayload(code="bad", message="went wrong", request_id="req-1"),
    "NoticePayload": NoticePayload(
        level="warning",
        code="quota",
        message="nearly out",
        details={"used": 90},
        url="https://hle.world/billing",
    ),
    # -- models.py --------------------------------------------------------
    "TunnelRegistration.minimal": TunnelRegistration(
        service_url="http://localhost:8080", service_label="ha", api_key="hle_" + "a" * 32
    ),
    "TunnelRegistration.full": TunnelRegistration(
        service_url="http://localhost:8080",
        service_label="ha",
        api_key="hle_" + "a" * 32,
        client_version="2608.1",
        protocol_version="1.5",
        websocket_enabled=False,
        auth_mode="sso",
        capabilities=["chunked_response"],
        zone="example.com",
        managed_by="hle-operator",
        webhook_path="/webhook/github",
        apex=True,
        options={"k": "v"},
    ),
    "TunnelRegistrationResponse": TunnelRegistrationResponse(
        tunnel_id="tun-1",
        subdomain="ha-x7k",
        public_url="https://ha-x7k.hle.world",
        websocket_enabled=True,
        user_code="x7k",
        service_label="ha",
        server_capabilities=["chunked_response"],
        zone="example.com",
    ),
    "ProxiedHttpRequest": ProxiedHttpRequest(
        request_id="req-1",
        method="POST",
        path="/api/x",
        headers={"content-type": "application/json"},
        body="Ymxh",
        query_string="a=1&b=2",
    ),
    "ProxiedHttpResponse": ProxiedHttpResponse(
        request_id="req-1",
        status_code=200,
        headers={"content-type": "application/json", "set-cookie": ["a=1", "b=2"]},
        body="Ymxh",
    ),
    "HttpResponseStart": HttpResponseStart(
        request_id="req-1", status_code=206, headers={"x": "y", "multi": ["a", "b"]}
    ),
    "HttpResponseChunk": HttpResponseChunk(request_id="req-1", chunk_index=3, data="Ymxh"),
    "HttpResponseEnd.ok": HttpResponseEnd(request_id="req-1"),
    "HttpResponseEnd.err": HttpResponseEnd(request_id="req-1", error="upstream died"),
    "WsStreamOpen": WsStreamOpen(stream_id="s1", path="/ws", headers={"origin": "x"}),
    "WsStreamAccept": WsStreamAccept(stream_id="s1", subprotocol="tty"),
    "WsStreamFrame": WsStreamFrame(stream_id="s1", data="Ymxh", is_binary=True),
    "WsStreamClose": WsStreamClose(
        stream_id="s1", code=1006, reason="abnormal", diagnostics={"frames_in": 42}
    ),
    "LogConfig": LogConfig(level="DEBUG", diagnostics=True),
    "DiagnosticEvent": DiagnosticEvent(event="ws.close", data={"code": 1006}, ts=1.5),
    "SpeedTestData": SpeedTestData(
        test_id="t1",
        direction="download",
        chunk_index=0,
        total_chunks=10,
        data="Ymxh",
        chunk_size_bytes=1024,
    ),
    "SpeedTestResult": SpeedTestResult(
        test_id="t1",
        direction="upload",
        total_bytes=1000,
        duration_seconds=1.25,
        throughput_mbps=6.4,
    ),
    # -- agent_protocol.py ------------------------------------------------
    "EndpointSpec": EndpointSpec(
        id=1,
        label="ha",
        service_url="http://localhost:8123",
        zone="example.com",
        auth_mode="sso",
        webhook_path="/hook",
        websocket_enabled=False,
    ),
    "AgentHello": AgentHello(token="hlea_x", agent_version="2608.1", capabilities=["fp"]),
    "AgentWelcome": AgentWelcome(
        agent_public_id="pub-1",
        base_domain="hle.world",
        api_key="hle_" + "a" * 32,
        endpoints=[EndpointSpec(id=1, label="ha", service_url="http://localhost:8123")],
        forward_rules=[_RULE],
    ),
    "AgentStateSync": AgentStateSync(
        endpoints=[EndpointSpec(id=2, label="tv", service_url="http://localhost:9")],
        forward_rules=None,
    ),
    "EndpointStatus": EndpointStatus(
        label="ha", connected=True, public_url="https://ha-x7k.hle.world", error=None
    ),
    "AgentStatus": AgentStatus(endpoints=[EndpointStatus(label="ha", connected=True)]),
    # -- fp_protocol.py ---------------------------------------------------
    "ForwardRule": _RULE,
    "ForwardRule.anyport": ForwardRule(host="192.168.1.50"),
    "FpOpen": FpOpen(stream_id="f1", target_host="localhost", target_port=22),
    "FpReady": FpReady(stream_id="f1"),
    "FpData": FpData(stream_id="f1", data="Ymxh"),
    "FpClose": FpClose(stream_id="f1", reason="eof"),
    "FpError": FpError(stream_id="f1", message="connection refused"),
    "FpHello": FpHello(api_key="hle_" + "a" * 32, agent="rpi", client_version="2608.1"),
    "FpWelcome": FpWelcome(agent_public_id="pub-1", allowed=["localhost:22"]),
    # -- discovery.py -----------------------------------------------------
    "DiscoveredService": DiscoveredService(
        provider="docker",
        id="svc-1",
        name="Jellyfin",
        address="http://localhost:8096",
        ports=[8096],
        namespace="media",
        labels={"app": "jellyfin"},
        already_exposed=True,
    ),
    "DiscoveryReport": DiscoveryReport(
        services=[
            DiscoveredService(provider="k8s", id="s2", name="ha", address="http://localhost:8123")
        ],
        providers=["k8s"],
        error=None,
    ),
    "DiscoveryRefresh": DiscoveryRefresh(),
    # -- preflight.py -----------------------------------------------------
    "PreflightFix": PreflightFix(
        field="service_url", value="https://192.168.1.1", label="Use https://192.168.1.1"
    ),
    "PreflightFinding": PreflightFinding(
        id="upstream_redirects_to_https",
        severity=Severity.ERROR,
        title="The service redirects to HTTPS on its own address",
        detail="The browser would leave the tunnel for a private address.",
        evidence="302 location: https://192.168.1.1/",
        fix=PreflightFix(field="service_url", value="https://192.168.1.1"),
    ),
    "PreflightRequest": PreflightRequest(
        request_id="req-1",
        service_url="http://192.168.1.1",
        tunnel_host="gw-ian.hle.world",
        verify_ssl=False,
        websocket_enabled=True,
        forward_host=False,
    ),
    "PreflightReport": PreflightReport(
        request_id="req-1",
        service_url="http://192.168.1.1",
        findings=[
            PreflightFinding(id="upstream_requires_auth", severity=Severity.INFO, title="Login")
        ],
        error=None,
        elapsed_ms=41.5,
        working_host_mode="upstream",
    ),
}
