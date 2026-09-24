"""The mappers read the relay's names once, and survive their absence."""

from __future__ import annotations

from hle_client.ops.models import (
    AccessRule,
    Agent,
    BasicAuthStatus,
    Daemon,
    PinStatus,
    ShareLink,
    Tunnel,
    TunnelDetail,
    kind_from_spec,
)


class TestTunnel:
    def test_is_active_becomes_online(self):
        assert Tunnel.from_api({"subdomain": "ha-x7k", "is_active": True}).online is True
        assert Tunnel.from_api({"subdomain": "ha-x7k", "is_active": False}).online is False

    def test_missing_keys_default(self):
        t = Tunnel.from_api({})
        assert t.subdomain == ""
        assert t.online is False
        assert t.websocket_enabled is True
        assert t.tunnel_id is None
        assert t.raw == {}

    def test_public_url_is_derived_when_absent(self):
        assert Tunnel.from_api({"subdomain": "ha-x7k"}).public_url == "https://ha-x7k.hle.world"
        t = Tunnel.from_api({"subdomain": "ha", "zone": "t00t.us"})
        assert t.public_url == "https://ha.t00t.us"

    def test_public_url_from_the_relay_wins(self):
        t = Tunnel.from_api({"subdomain": "ha-x7k", "public_url": "https://x.example"})
        assert t.public_url == "https://x.example"

    def test_label_is_the_subdomain_without_the_user_code(self):
        t = Tunnel.from_api({"subdomain": "home-assistant-x7k"}, user_code="x7k")
        assert t.label == "home-assistant"
        assert Tunnel.from_api({"subdomain": "ha-x7k"}).label is None

    def test_raw_is_kept_for_json_output(self):
        payload = {"subdomain": "ha-x7k", "total_http_requests": 12}
        assert Tunnel.from_api(payload).raw == payload

    def test_frozen(self):
        import dataclasses

        t = Tunnel.from_api({"subdomain": "ha-x7k"})
        try:
            t.online = True  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            return
        raise AssertionError("Tunnel should be frozen")


class TestTunnelDetail:
    def test_full_status_payload(self):
        d = TunnelDetail.from_api(
            {
                "subdomain": "ha-x7k",
                "public_url": "https://ha-x7k.hle.world",
                "is_active": True,
                "auth_mode": "sso",
                "access_rules": [{"id": 1, "allowed_email": "a@x.com", "provider": "google"}],
                "pin": {"has_pin": True, "updated_at": "2026-01-01"},
                "basic_auth": {"enabled": True, "username": "bob"},
                "is_protected": True,
            }
        )
        assert d.tunnel.online is True
        assert d.access_rules[0].email == "a@x.com"
        assert d.pin.enabled is True
        assert d.basic_auth.username == "bob"
        assert d.protected is True

    def test_missing_gates_default_off(self):
        d = TunnelDetail.from_api({"subdomain": "ha-x7k"})
        assert d.access_rules == ()
        assert d.pin.enabled is False
        assert d.basic_auth.enabled is False
        assert d.protected is False


class TestAccessRule:
    def test_allowed_email_becomes_email(self):
        r = AccessRule.from_api({"id": 3, "allowed_email": "A@X.com", "provider": "github"})
        assert (r.id, r.email, r.provider) == (3, "A@X.com", "github")
        assert r.key == ("a@x.com", "github")

    def test_missing_provider_is_any(self):
        assert AccessRule.from_api({"allowed_email": "a@x.com"}).provider == "any"


class TestSmallStatuses:
    def test_pin(self):
        assert PinStatus.from_api({"has_pin": True}).enabled is True
        assert PinStatus.from_api({}).enabled is False

    def test_basic_auth(self):
        s = BasicAuthStatus.from_api({"enabled": True, "username": "u"})
        assert (s.enabled, s.username) == (True, "u")
        assert BasicAuthStatus.from_api({}).enabled is False

    def test_share_link_list_shape(self):
        s = ShareLink.from_api(
            {"id": 1, "label": "bob", "token_prefix": "abc", "use_count": 2, "is_active": False}
        )
        assert (s.id, s.label, s.token_prefix, s.use_count, s.active) == (1, "bob", "abc", 2, False)
        assert s.share_url is None

    def test_share_link_create_shape(self):
        s = ShareLink.from_api(
            {"share_url": "https://x/s/abc", "link": {"id": 2, "expires_at": "soon", "max_uses": 5}}
        )
        assert (s.id, s.share_url, s.expires_at, s.max_uses) == (2, "https://x/s/abc", "soon", 5)


class TestAgent:
    """The relay sends ``online``; nothing here may read ``is_online``."""

    def test_online_is_read_from_online(self):
        assert Agent.from_api({"name": "rpi", "online": True}).online is True
        assert Agent.from_api({"name": "rpi", "is_online": True}).online is False

    def test_state_words(self):
        assert Agent.from_api({"name": "a", "online": True}).state == "online"
        assert Agent.from_api({"name": "a", "online": False}).state == "offline"
        assert Agent.from_api({"name": "a", "online": True, "is_active": False}).state == "disabled"

    def test_field_renames_and_defaults(self):
        a = Agent.from_api(
            {
                "public_id": "ag_1",
                "name": "rpi",
                "endpoint_count": 4,
                "agent_version": "2609.7",
                "last_seen_at": "2026-09-24T00:00:00Z",
            }
        )
        assert (a.id, a.endpoints, a.version, a.last_seen) == (
            "ag_1",
            4,
            "2609.7",
            "2026-09-24T00:00:00Z",
        )
        empty = Agent.from_api({})
        assert (empty.name, empty.endpoints, empty.version, empty.enabled) == ("?", 0, None, True)


class TestDaemon:
    def test_kind_from_spec(self):
        assert kind_from_spec({"run_args": ["agent", "run"]}) == "agent"
        assert kind_from_spec({"run_args": ["expose", "--service", "x"]}) == "tunnel"
        assert kind_from_spec({"run_args": ["fp", "rpi", "22"]}) == "forward"
        assert kind_from_spec(None) == "unknown"

    def test_from_spec(self):
        d = Daemon.from_spec(
            "hle-agent.service", user_mode=True, spec={"label": "agent", "run_args": ["agent"]}
        )
        assert (d.scope, d.user_mode, d.kind, d.label) == ("user", True, "agent", "agent")
