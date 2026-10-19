"""Tests for service URL scheme normalization in TunnelConfig."""

from __future__ import annotations

from hle_client.tunnel import TunnelConfig


class TestServiceUrlNormalization:
    def test_schemeless_host_port_gets_http(self):
        # Regression: "localhost:9998" made httpx treat "localhost" as the
        # protocol — the tunnel registered fine but every forwarded request
        # failed with UnsupportedProtocol / "Bad Gateway: unexpected error".
        cfg = TunnelConfig(service_url="localhost:9998", service_label="tv")
        assert cfg.service_url == "http://localhost:9998"

    def test_schemeless_ip_port_gets_http(self):
        cfg = TunnelConfig(service_url="192.168.1.10:8123", service_label="ha")
        assert cfg.service_url == "http://192.168.1.10:8123"

    def test_http_url_unchanged(self):
        cfg = TunnelConfig(service_url="http://localhost:7681", service_label="ssh")
        assert cfg.service_url == "http://localhost:7681"

    def test_https_url_unchanged(self):
        cfg = TunnelConfig(service_url="https://192.168.100.200:8006/", service_label="prox")
        assert cfg.service_url == "https://192.168.100.200:8006/"

    def test_empty_url_unchanged(self):
        cfg = TunnelConfig(service_url="", service_label="x")
        assert cfg.service_url == ""

    def test_url_userinfo_moves_to_upstream_basic_auth(self):
        # The relay stores and displays ``service_url`` with the tunnel, so
        # URL-embedded credentials must never leave the host. They are kept
        # locally as upstream Basic auth instead.
        cfg = TunnelConfig(service_url="http://u:p@h:80/", service_label="x")
        assert cfg.service_url == "http://h:80/"
        assert cfg.upstream_basic_auth == ("u", "p")

    def test_percent_encoded_userinfo_is_decoded_like_before(self):
        cfg = TunnelConfig(service_url="http://us%40er:p%3Aw%2Fd@h:80/", service_label="x")
        assert cfg.service_url == "http://h:80/"
        assert cfg.upstream_basic_auth == ("us@er", "p:w/d")

    def test_url_empty_username_still_moves_the_password(self):
        cfg = TunnelConfig(service_url="http://:p@h:80", service_label="x")
        assert cfg.service_url == "http://h:80"
        assert cfg.upstream_basic_auth == ("", "p")

    def test_explicit_upstream_basic_auth_wins_over_url_credentials(self):
        cfg = TunnelConfig(
            service_url="http://u:p@h:80",
            service_label="x",
            upstream_basic_auth=("a", "b"),
        )
        assert cfg.service_url == "http://h:80"
        assert cfg.upstream_basic_auth == ("a", "b")

    def test_schemeless_url_with_userinfo_is_normalised_and_stripped(self):
        cfg = TunnelConfig(service_url="u:p@h:9998", service_label="x")
        assert cfg.service_url == "http://h:9998"
        assert cfg.upstream_basic_auth == ("u", "p")
