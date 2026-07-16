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
        cfg = TunnelConfig(service_url="https://192.168.2.200:8006/", service_label="prox")
        assert cfg.service_url == "https://192.168.2.200:8006/"

    def test_empty_url_unchanged(self):
        cfg = TunnelConfig(service_url="", service_label="x")
        assert cfg.service_url == ""
