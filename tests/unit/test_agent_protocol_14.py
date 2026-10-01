"""Agent protocol 1.4 and discovery 1.1.

Additive only: a Kubernetes Service target on EndpointSpec, the
declared_endpoints / declared_ack frames, logs_request / logs_response, the
k8s:* and logs hello capabilities, handover_group, and extra optional fields on
DiscoveredService. Everything here is proved round-trippable and ignorable by a
peer that predates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hle_common.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    K8S_DECLARED_CAPABILITY,
    K8S_SERVICES_CAPABILITY,
    LOGS_CAPABILITY,
    MAX_DECLARED_ENDPOINTS,
    MAX_LOG_LINE_CHARS,
    MAX_LOG_LINES,
    AgentHello,
    AgentMsgType,
    AgentStateSync,
    DeclaredAck,
    DeclaredAckEntry,
    DeclaredEndpoint,
    DeclaredEndpoints,
    EndpointSpec,
    K8sServiceTarget,
    LogsRequest,
    LogsResponse,
)
from hle_common.discovery import (
    DISCOVERY_PROTOCOL_VERSION,
    DiscoveredService,
)
from hle_common.wire_samples import SAMPLES

_BASELINE: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "wire_baseline.json").read_text()
)

# The EndpointSpec keys a 1.3 peer knows -- everything before 1.4's `target`.
_ENDPOINT_1_3_FIELDS = {
    "id",
    "label",
    "service_url",
    "zone",
    "auth_mode",
    "webhook_path",
    "websocket_enabled",
    "verify_ssl",
    "forward_host",
    "upstream_basic_auth",
    "apex",
    "options",
    "response_timeout",
    "managed_by",
}


class TestVersions:
    def test_agent_protocol_is_1_4(self):
        assert AGENT_PROTOCOL_VERSION == "1.4"

    def test_discovery_is_1_1(self):
        assert DISCOVERY_PROTOCOL_VERSION == "1.1"


class TestNewMessageTypes:
    def test_registered(self):
        assert AgentMsgType.DECLARED_ENDPOINTS == "declared_endpoints"
        assert AgentMsgType.DECLARED_ACK == "declared_ack"
        assert AgentMsgType.LOGS_REQUEST == "logs_request"
        assert AgentMsgType.LOGS_RESPONSE == "logs_response"

    def test_models_default_to_their_registered_type(self):
        assert DeclaredEndpoints().type == "declared_endpoints"
        assert DeclaredAck().type == "declared_ack"
        assert LogsRequest(request_id="r").type == "logs_request"
        assert LogsResponse(request_id="r").type == "logs_response"


class TestCapabilityConstants:
    def test_values(self):
        assert K8S_SERVICES_CAPABILITY == "k8s:services"
        assert K8S_DECLARED_CAPABILITY == "k8s:declared"
        assert LOGS_CAPABILITY == "logs"

    def test_v1_4_hello_carries_them(self):
        hello = json.loads(_BASELINE["AgentHello.v1_4"])
        assert {
            K8S_SERVICES_CAPABILITY,
            K8S_DECLARED_CAPABILITY,
            LOGS_CAPABILITY,
        } <= set(hello["capabilities"])


class TestK8sServiceTarget:
    def test_defaults(self):
        target = K8sServiceTarget(namespace="media", name="jellyfin", port=8096)
        assert target.kind == "k8s_service"
        assert target.scheme == "http"

    def test_namespace_name_and_port_are_required(self):
        with pytest.raises(ValueError):
            K8sServiceTarget.model_validate({"name": "x", "port": 80})
        with pytest.raises(ValueError):
            K8sServiceTarget.model_validate({"namespace": "n", "port": 80})
        with pytest.raises(ValueError):
            K8sServiceTarget.model_validate({"namespace": "n", "name": "x"})

    @pytest.mark.parametrize("port", [8096, 1, 65535, "http", "postgres", "http-admin"])
    def test_port_is_a_number_or_a_name(self, port):
        target = K8sServiceTarget(namespace="media", name="jellyfin", port=port)
        again = K8sServiceTarget.model_validate_json(target.model_dump_json())
        assert again.port == port
        assert type(again.port) is type(port)

    @pytest.mark.parametrize(
        "port",
        [
            0,
            65536,
            -1,
            True,
            False,
            "",
            "8096",  # a numeric string is the wrong shape, not a port name
            "12345",
            "HTTP",
            "http_port",
            "http-",
            "-http",
            "http--admin",
            "http.",
            "1234567890123456",  # >15 chars
            "8080/",  # not a name
        ],
    )
    def test_bad_ports_are_rejected(self, port):
        with pytest.raises(ValueError):
            K8sServiceTarget(namespace="media", name="jellyfin", port=port)

    @pytest.mark.parametrize("namespace", ["media", "a", "media-1", "m" * 63])
    def test_valid_namespaces(self, namespace):
        K8sServiceTarget(namespace=namespace, name="jellyfin", port=80)

    @pytest.mark.parametrize("namespace", ["", "Media", "media_1", "-media", "media-", "m" * 64])
    def test_bad_namespaces_are_rejected(self, namespace):
        with pytest.raises(ValueError):
            K8sServiceTarget(namespace=namespace, name="jellyfin", port=80)

    @pytest.mark.parametrize("name", ["jellyfin", "a", "jelly-1", "j" * 63])
    def test_valid_names(self, name):
        K8sServiceTarget(namespace="media", name=name, port=80)

    @pytest.mark.parametrize("name", ["", "1jellyfin", "Jelly", "jelly_1", "-x", "x-", "x" * 64])
    def test_bad_names_are_rejected(self, name):
        with pytest.raises(ValueError):
            K8sServiceTarget(namespace="media", name=name, port=80)

    def test_bad_kind_is_rejected(self):
        with pytest.raises(ValueError):
            K8sServiceTarget.model_validate(
                {"kind": "k8s_ingress", "namespace": "media", "name": "jellyfin", "port": 80}
            )

    def test_bad_scheme_is_rejected(self):
        with pytest.raises(ValueError):
            K8sServiceTarget.model_validate(
                {"namespace": "media", "name": "jellyfin", "port": 80, "scheme": "ftp"}
            )

    def test_https_scheme_round_trips(self):
        target = K8sServiceTarget(namespace="media", name="jellyfin", port="https", scheme="https")
        again = K8sServiceTarget.model_validate_json(target.model_dump_json())
        assert again.scheme == "https"


class TestEndpointSpecTarget:
    def test_target_defaults_to_none(self):
        ep = EndpointSpec(id=1, label="a", service_url="http://x")
        assert ep.target is None

    def test_1_3_endpoint_parses_with_no_target(self):
        ep = EndpointSpec.model_validate_json(_BASELINE["EndpointSpec.v1_3"])
        assert ep.target is None

    def test_target_round_trips(self):
        ep = EndpointSpec.model_validate_json(_BASELINE["EndpointSpec.v1_4"])
        assert ep.target is not None
        assert (ep.target.namespace, ep.target.name, ep.target.port) == ("media", "jellyfin", 8096)
        again = EndpointSpec.model_validate_json(ep.model_dump_json())
        assert again.target == ep.target

    def test_1_4_endpoint_differs_from_1_3_only_by_target(self):
        new = json.loads(_BASELINE["EndpointSpec.v1_4"])
        assert set(new) - set(_ENDPOINT_1_3_FIELDS) == {"target"}
        assert new["target"] == {
            "kind": "k8s_service",
            "namespace": "media",
            "name": "jellyfin",
            "port": 8096,
            "scheme": "http",
        }

    def test_1_4_endpoint_projects_down_for_a_1_3_peer(self):
        """A 1.3 peer keeps the fields it declares and drops target."""
        full = json.loads(_BASELINE["EndpointSpec.v1_4"])
        projected = {k: v for k, v in full.items() if k in _ENDPOINT_1_3_FIELDS}
        ep = EndpointSpec.model_validate(projected)
        assert ep.target is None
        assert ep.service_url == full["service_url"]

    @pytest.mark.parametrize("name", ["AgentWelcome", "AgentStateSync"])
    def test_nested_endpoints_gain_only_a_null_target(self, name):
        for ep in json.loads(_BASELINE[name])["endpoints"]:
            assert set(ep) == _ENDPOINT_1_3_FIELDS | {"target"}
            assert ep["target"] is None

    def test_state_sync_with_a_target_round_trips(self):
        raw = json.dumps(
            {"type": "state_sync", "endpoints": [json.loads(_BASELINE["EndpointSpec.v1_4"])]}
        )
        sync = AgentStateSync.model_validate_json(raw)
        assert sync.endpoints[0].target is not None
        again = AgentStateSync.model_validate_json(sync.model_dump_json())
        assert again.endpoints == sync.endpoints


class TestDeclaredEndpoints:
    def test_declared_endpoint_requires_label_and_source_ref(self):
        with pytest.raises(ValueError):
            DeclaredEndpoint.model_validate({"source_ref": "hletunnel:ns/x"})
        with pytest.raises(ValueError):
            DeclaredEndpoint.model_validate({"label": "x"})

    def test_defaults(self):
        ep = DeclaredEndpoint(label="x", source_ref="hletunnel:ns/x", service_url="http://x:80")
        assert ep.target is None
        assert ep.sync_policy == "strict"
        assert ep.service_url == "http://x:80"
        assert ep.auth_mode == "sso"
        assert ep.upstream_basic_auth is None
        assert ep.upstream_basic_auth_secret is None

    def test_requires_a_target_or_a_service_url(self):
        with pytest.raises(ValueError):
            DeclaredEndpoint(label="x", source_ref="hletunnel:ns/x")
        with pytest.raises(ValueError):
            DeclaredEndpoint(label="x", source_ref="hletunnel:ns/x", service_url="")
        DeclaredEndpoint(
            label="x",
            source_ref="hletunnel:ns/x",
            target=K8sServiceTarget(namespace="ns", name="x", port=80),
        )
        DeclaredEndpoint(label="x", source_ref="hletunnel:ns/x", service_url="http://x:80")

    def test_upstream_basic_auth_is_rejected(self):
        with pytest.raises(ValueError, match="upstream_basic_auth"):
            DeclaredEndpoint(
                label="x",
                source_ref="hletunnel:ns/x",
                service_url="http://x:80",
                upstream_basic_auth="user:pass",
            )

    def test_upstream_basic_auth_secret_round_trips(self):
        ep = DeclaredEndpoint(
            label="x",
            source_ref="hletunnel:ns/x",
            service_url="http://x:80",
            upstream_basic_auth_secret="media/db-auth#password",
        )
        again = DeclaredEndpoint.model_validate_json(ep.model_dump_json())
        assert again.upstream_basic_auth_secret == "media/db-auth#password"

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "media#password",
            "media/db-auth",
            "/db-auth#password",
            "media/#password",
            "media/db-auth#",
            "Media/db-auth#password",
            "media/db_auth#password",
            "a/b#c#d",
        ],
    )
    def test_bad_upstream_basic_auth_secret_is_rejected(self, ref):
        with pytest.raises(ValueError):
            DeclaredEndpoint(
                label="x",
                source_ref="hletunnel:ns/x",
                service_url="http://x:80",
                upstream_basic_auth_secret=ref,
            )

    @pytest.mark.parametrize("policy", ["strict", "initial"])
    def test_valid_sync_policies(self, policy):
        ep = DeclaredEndpoint(
            label="x", source_ref="hletunnel:ns/x", service_url="http://x:80", sync_policy=policy
        )
        assert ep.sync_policy == policy

    def test_bad_sync_policy_is_rejected(self):
        with pytest.raises(ValueError, match="sync_policy"):
            DeclaredEndpoint.model_validate(
                {
                    "label": "x",
                    "source_ref": "hletunnel:ns/x",
                    "service_url": "http://x:80",
                    "sync_policy": "always",
                }
            )

    @pytest.mark.parametrize(
        "ref", ["hletunnel:ns/name", "ingress:ns/name", "ingress:media/jelly-fin"]
    )
    def test_valid_source_refs(self, ref):
        DeclaredEndpoint(label="x", source_ref=ref, service_url="http://x:80")

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "ns/name",
            "hletunnel:ns",
            "hletunnel:/name",
            "hletunnel:ns/",
            "other:ns/name",
            "hletunnel:NS/name",
            "hletunnel:ns/Name",
        ],
    )
    def test_bad_source_refs_are_rejected(self, ref):
        with pytest.raises(ValueError, match="source_ref"):
            DeclaredEndpoint(label="x", source_ref=ref, service_url="http://x:80")

    def test_sync_policy_initial_round_trips(self):
        ep = DeclaredEndpoint(
            label="x", source_ref="ingress:ns/x", service_url="http://x:80", sync_policy="initial"
        )
        again = DeclaredEndpoint.model_validate_json(ep.model_dump_json())
        assert again.sync_policy == "initial"
        assert again.target is None
        assert again.service_url == "http://x:80"

    def test_target_round_trips(self):
        ep = DeclaredEndpoint(
            label="jellyfin",
            target=K8sServiceTarget(namespace="media", name="jellyfin", port=8096),
            source_ref="hletunnel:media/jellyfin",
            auth_mode="sso",
        )
        again = DeclaredEndpoint.model_validate_json(ep.model_dump_json())
        assert again == ep

    def test_frame_round_trips(self):
        frame = DeclaredEndpoints(
            revision=3,
            endpoints=[
                DeclaredEndpoint(
                    label="jellyfin",
                    target=K8sServiceTarget(namespace="media", name="jellyfin", port=8096),
                    source_ref="hletunnel:media/jellyfin",
                )
            ],
        )
        again = DeclaredEndpoints.model_validate_json(frame.model_dump_json())
        assert again.endpoints == frame.endpoints
        assert again.revision == 3

    def test_empty_frame_round_trips(self):
        frame = DeclaredEndpoints()
        assert DeclaredEndpoints.model_validate_json(frame.model_dump_json()).endpoints == []
        assert frame.revision == 0

    @pytest.mark.parametrize("revision", [-1, True, 1.5, "1"])
    def test_bad_revision_is_rejected(self, revision):
        with pytest.raises(ValueError, match="revision"):
            DeclaredEndpoints(revision=revision)

    def test_too_many_endpoints_are_rejected(self):
        def ep(i):
            return DeclaredEndpoint(
                label=f"x{i}", source_ref="hletunnel:ns/x", service_url="http://x:80"
            )

        DeclaredEndpoints(endpoints=[ep(i) for i in range(MAX_DECLARED_ENDPOINTS)])
        with pytest.raises(ValueError, match="at most"):
            DeclaredEndpoints(endpoints=[ep(i) for i in range(MAX_DECLARED_ENDPOINTS + 1)])


class TestDeclaredAck:
    def test_status_defaults_to_accepted(self):
        assert DeclaredAckEntry(label="x").status == "accepted"

    def test_bad_status_is_rejected(self):
        with pytest.raises(ValueError, match="status"):
            DeclaredAckEntry.model_validate({"label": "x", "status": "rejected"})

    def test_conflict_carries_a_message(self):
        ack = DeclaredAck(
            revision=7,
            endpoints=[
                DeclaredAckEntry(label="a", status="accepted"),
                DeclaredAckEntry(label="b", status="conflict", message="managed in the dashboard"),
            ],
        )
        again = DeclaredAck.model_validate_json(ack.model_dump_json())
        assert [e.status for e in again.endpoints] == ["accepted", "conflict"]
        assert again.endpoints[1].message == "managed in the dashboard"

    def test_revision_defaults_to_zero_and_echoes(self):
        assert DeclaredAck().revision == 0
        frame = DeclaredEndpoints(revision=9)
        ack = DeclaredAck(revision=frame.revision)
        assert DeclaredAck.model_validate_json(ack.model_dump_json()).revision == 9

    @pytest.mark.parametrize("revision", [-1, True, 1.5, "1"])
    def test_bad_revision_is_rejected(self, revision):
        with pytest.raises(ValueError, match="revision"):
            DeclaredAck(revision=revision)


class TestLogs:
    def test_bounds(self):
        assert MAX_LOG_LINES == 500
        assert MAX_LOG_LINE_CHARS == 2000

    def test_request_defaults(self):
        req = LogsRequest(request_id="log-1")
        assert req.lines == 200

    def test_request_round_trips(self):
        req = LogsRequest(request_id="log-1", lines=MAX_LOG_LINES)
        again = LogsRequest.model_validate_json(req.model_dump_json())
        assert again.request_id == "log-1"
        assert again.lines == 500

    @pytest.mark.parametrize("lines", [0, -5, -1])
    def test_request_clamps_low(self, lines):
        assert LogsRequest(request_id="r", lines=lines).lines == 1

    @pytest.mark.parametrize("lines", [MAX_LOG_LINES + 1, 10_000])
    def test_request_clamps_high(self, lines):
        assert LogsRequest(request_id="r", lines=lines).lines == MAX_LOG_LINES

    @pytest.mark.parametrize("lines", [True, False, 1.5, "10", None])
    def test_request_rejects_non_ints(self, lines):
        with pytest.raises(ValueError, match="lines"):
            LogsRequest(request_id="r", lines=lines)

    def test_response_defaults(self):
        res = LogsResponse(request_id="log-1")
        assert res.lines == []
        assert res.truncated is False

    def test_response_lines_are_not_shared(self):
        a = LogsResponse(request_id="r")
        b = LogsResponse(request_id="r")
        a.lines.append("x")
        assert b.lines == []

    def test_response_accepts_the_limits(self):
        res = LogsResponse(
            request_id="r",
            lines=["x" * MAX_LOG_LINE_CHARS] * MAX_LOG_LINES,
        )
        assert len(res.lines) == MAX_LOG_LINES

    def test_response_rejects_too_many_lines(self):
        with pytest.raises(ValueError, match="at most"):
            LogsResponse(request_id="r", lines=["x"] * (MAX_LOG_LINES + 1))

    def test_response_rejects_a_long_line(self):
        with pytest.raises(ValueError, match="characters"):
            LogsResponse(request_id="r", lines=["x" * (MAX_LOG_LINE_CHARS + 1)])

    def test_response_round_trips(self):
        res = LogsResponse(request_id="log-1", lines=["a", "b"], truncated=True)
        again = LogsResponse.model_validate_json(res.model_dump_json())
        assert again.lines == ["a", "b"]
        assert again.truncated is True


class TestHelloHandoverGroup:
    def test_defaults_to_none(self):
        assert AgentHello(token="t").handover_group is None

    def test_round_trips(self):
        hello = AgentHello(token="t", handover_group="hle/hle-operator")
        again = AgentHello.model_validate_json(hello.model_dump_json())
        assert again.handover_group == "hle/hle-operator"

    def test_a_1_2_hello_parses_with_no_group(self):
        hello = AgentHello.model_validate_json(_BASELINE["AgentHello.v1_2"])
        assert hello.handover_group is None


class TestDiscovery11Fields:
    def test_new_fields_default_to_none(self):
        svc = DiscoveredService(provider="k8s", id="x", name="x", address="http://x")
        for name in ("port_name", "app_protocol", "ready_endpoints", "exposed_by"):
            assert getattr(svc, name) is None

    def test_round_trips(self):
        svc = DiscoveredService.model_validate_json(_BASELINE["DiscoveredService.v1_1"])
        assert svc.port_name == "http"
        assert svc.app_protocol == "http"
        assert svc.ready_endpoints == 2
        assert svc.exposed_by == "hletunnel:media/jellyfin"
        assert DiscoveredService.model_validate_json(svc.model_dump_json()) == svc

    def test_ready_endpoints_zero_is_not_unknown(self):
        svc = DiscoveredService(
            provider="k8s", id="x", name="x", address="http://x", ready_endpoints=0
        )
        assert svc.ready_endpoints == 0
        assert DiscoveredService.model_validate_json(svc.model_dump_json()).ready_endpoints == 0

    def test_old_service_projects_down_for_a_1_0_server(self):
        old_fields = {
            "provider",
            "id",
            "name",
            "address",
            "ports",
            "namespace",
            "labels",
            "already_exposed",
        }
        full = json.loads(_BASELINE["DiscoveredService.v1_1"])
        projected = {k: v for k, v in full.items() if k in old_fields}
        svc = DiscoveredService.model_validate(projected)
        assert svc.port_name is None
        assert svc.exposed_by is None


class TestOldShapesStillParse:
    """Frames and fields that predate this change must still validate."""

    def test_declared_endpoint_without_the_secret_field(self):
        ep = DeclaredEndpoint.model_validate(
            {
                "label": "x",
                "source_ref": "hletunnel:ns/x",
                "service_url": "http://x:80",
                "target": None,
                "sync_policy": "strict",
            }
        )
        assert ep.upstream_basic_auth_secret is None

    def test_declared_endpoints_frame_without_revision(self):
        raw = {
            "type": "declared_endpoints",
            "endpoints": [
                {
                    "label": "x",
                    "source_ref": "hletunnel:ns/x",
                    "service_url": "http://x:80",
                }
            ],
        }
        frame = DeclaredEndpoints.model_validate(raw)
        assert frame.revision == 0
        assert frame.endpoints[0].label == "x"

    def test_declared_ack_frame_without_revision(self):
        ack = DeclaredAck.model_validate(
            {"type": "declared_ack", "endpoints": [{"label": "x", "status": "accepted"}]}
        )
        assert ack.revision == 0
        assert ack.endpoints[0].status == "accepted"

    def test_a_1_3_endpoint_parses(self):
        ep = EndpointSpec.model_validate_json(_BASELINE["EndpointSpec.v1_3"])
        assert ep.target is None

    def test_a_1_2_hello_parses(self):
        assert AgentHello.model_validate_json(_BASELINE["AgentHello.v1_2"]).handover_group is None


class TestNewFramesIgnoreFieldsFromTheFuture:
    @pytest.mark.parametrize(
        "name",
        [
            "K8sServiceTarget",
            "EndpointSpec.v1_4",
            "DeclaredEndpoint.target",
            "DeclaredEndpoint.secret",
            "DeclaredEndpoints",
            "DeclaredAck",
            "LogsRequest",
            "LogsResponse",
            "AgentHello.v1_4",
            "DiscoveredService.v1_1",
        ],
    )
    def test_unknown_keys_are_dropped(self, name):
        model = SAMPLES[name]
        raw = json.loads(_BASELINE[name])
        raw["a_1_5_field"] = {"nested": True}
        assert type(model).model_validate(raw).model_dump_json() == _BASELINE[name]
