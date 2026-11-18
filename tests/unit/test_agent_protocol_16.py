"""Agent protocol 1.6: allowed visitor IP ranges on a declaration's access.

Additive only: ``DeclaredAccess`` gains ``allowed_ips`` and ``allow_origin_ip``,
so the Kubernetes operator can declare which visitor addresses skip sign-in.
Everything here is proved round-trippable, validated, and ignorable by a peer
that predates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hle_common.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    AllowedIp,
    DeclaredAccess,
    DeclaredEndpoint,
    DeclaredEndpoints,
    K8sServiceTarget,
)

_BASELINE: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "wire_baseline.json").read_text()
)


def _endpoint(**kw) -> DeclaredEndpoint:
    return DeclaredEndpoint(
        label="photos",
        target=K8sServiceTarget(namespace="media", name="photos", port=2283),
        source_ref="hletunnel:media/photos",
        **kw,
    )


class TestVersion:
    def test_is_at_least_1_6(self):
        # Pinned exactly by the newest protocol's own tests (1.7 at present).
        assert tuple(int(p) for p in AGENT_PROTOCOL_VERSION.split(".")) >= (1, 6)


class TestAllowedIp:
    def test_label_defaults_to_none(self):
        assert AllowedIp(cidr="203.0.113.7").label is None

    @pytest.mark.parametrize("cidr", ["203.0.113.7", "203.0.113.0/24", "2001:db8::/32", "x" * 64])
    def test_cidr_is_checked_for_length_only(self, cidr):
        # Range validity is the receiver's job; the model bounds the string only.
        assert AllowedIp(cidr=cidr).cidr == cidr

    def test_empty_cidr_is_rejected(self):
        with pytest.raises(ValueError, match="cidr"):
            AllowedIp(cidr="")

    def test_over_long_cidr_is_rejected(self):
        with pytest.raises(ValueError, match="cidr"):
            AllowedIp(cidr="x" * 65)

    def test_label_at_the_limit_is_accepted(self):
        assert AllowedIp(cidr="203.0.113.7", label="x" * 60).label == "x" * 60

    def test_over_long_label_is_rejected(self):
        with pytest.raises(ValueError, match="label"):
            AllowedIp(cidr="203.0.113.7", label="x" * 61)

    def test_round_trips(self):
        ip = AllowedIp(cidr="203.0.113.0/24", label="office")
        assert AllowedIp.model_validate_json(ip.model_dump_json()) == ip
        bare = AllowedIp(cidr="198.51.100.7")
        assert AllowedIp.model_validate_json(bare.model_dump_json()).label is None


class TestDeclaredAccess:
    def test_defaults_are_empty_and_false(self):
        access = DeclaredAccess()
        assert access.allowed_ips == []
        assert access.allow_origin_ip is False

    def test_a_1_5_access_without_the_fields_parses(self):
        # Exactly what a 1.5 peer sends: neither key is present.
        access = DeclaredAccess.model_validate(
            {"allowed_users": [], "pin": None, "basic_auth": None}
        )
        assert access.allowed_ips == []
        assert access.allow_origin_ip is False

    def test_lists_are_not_shared(self):
        first = DeclaredAccess()
        second = DeclaredAccess()
        first.allowed_ips.append(AllowedIp(cidr="203.0.113.7"))
        assert second.allowed_ips == []

    def test_round_trips(self):
        access = DeclaredAccess(
            allowed_ips=[
                AllowedIp(cidr="203.0.113.0/24", label="office"),
                AllowedIp(cidr="198.51.100.7"),
            ],
            allow_origin_ip=True,
        )
        again = DeclaredAccess.model_validate_json(access.model_dump_json())
        assert again == access
        assert again.allowed_ips[0].label == "office"
        assert again.allowed_ips[1].label is None
        assert again.allow_origin_ip is True

    def test_secrets_stay_masked_with_ip_fields_set(self):
        access = DeclaredAccess(
            pin="9876",
            basic_auth="visitor:hunter2",
            allowed_ips=[AllowedIp(cidr="203.0.113.7", label="home")],
            allow_origin_ip=True,
        )
        text = repr(access)
        assert "9876" not in text
        assert "hunter2" not in text
        assert "203.0.113.7" in text


class TestDeclaredEndpointsRoundTrip:
    def test_frame_carries_the_new_fields(self):
        endpoint = _endpoint(
            access=DeclaredAccess(
                allowed_ips=[AllowedIp(cidr="203.0.113.0/24", label="office")],
                allow_origin_ip=True,
            ),
        )
        frame = DeclaredEndpoints(revision=4, endpoints=[endpoint])
        again = DeclaredEndpoints.model_validate_json(frame.model_dump_json())
        assert again == frame
        access = again.endpoints[0].access
        assert access is not None
        assert access.allowed_ips == [AllowedIp(cidr="203.0.113.0/24", label="office")]
        assert access.allow_origin_ip is True

    def test_a_1_5_frame_loads_with_empty_defaults(self):
        frame = DeclaredEndpoints(
            revision=2, endpoints=[_endpoint(access=DeclaredAccess(pin="1234"))]
        )
        raw = frame.model_dump()
        access_raw = raw["endpoints"][0]["access"]
        del access_raw["allowed_ips"]
        del access_raw["allow_origin_ip"]
        access = DeclaredEndpoints.model_validate(raw).endpoints[0].access
        assert access is not None
        assert access.allowed_ips == []
        assert access.allow_origin_ip is False

    def test_old_frame_without_access_parses(self):
        raw = json.loads(_endpoint().model_dump_json())
        del raw["access"]
        assert DeclaredEndpoint.model_validate(raw).access is None


class TestUnknownFieldsAreIgnored:
    """The new models keep the protocol's rule: keys from the future are dropped."""

    def test_declared_access_ignores_a_future_key(self):
        access = DeclaredAccess(
            allowed_ips=[AllowedIp(cidr="203.0.113.0/24", label="office")],
            allow_origin_ip=True,
        )
        raw = json.loads(access.model_dump_json())
        raw["a_1_7_field"] = {"nested": True}
        assert DeclaredAccess.model_validate(raw).model_dump_json() == access.model_dump_json()

    def test_allowed_ip_ignores_a_future_key(self):
        ip = AllowedIp(cidr="203.0.113.7", label="home")
        raw = json.loads(ip.model_dump_json())
        raw["a_1_7_field"] = {"nested": True}
        assert AllowedIp.model_validate(raw).model_dump_json() == ip.model_dump_json()

    def test_the_wire_sample_pins_the_new_fields(self):
        raw = json.loads(_BASELINE["DeclaredEndpoint.access"])["access"]
        assert set(raw) == {
            "allowed_users",
            "pin",
            "basic_auth",
            "allowed_ips",
            "allow_origin_ip",
        }
        assert raw["allowed_ips"] == [
            {"cidr": "203.0.113.0/24", "label": "office"},
            {"cidr": "198.51.100.7", "label": None},
        ]
        assert raw["allow_origin_ip"] is True
