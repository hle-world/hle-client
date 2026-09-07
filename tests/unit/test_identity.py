"""Tests for the per-process client identity and the shared close codes."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from hle_client import identity
from hle_common import close_codes


class TestInstanceId:
    def test_stable_within_a_process(self):
        """The relay uses this to recognise the same client reconnecting."""
        assert identity.instance_id() == identity.instance_id()

    def test_looks_like_an_id_and_carries_no_host_detail(self):
        value = identity.instance_id()
        assert len(value) == 32
        assert all(c in "0123456789abcdef" for c in value)

    def test_a_new_process_gets_a_new_id(self):
        """Deliberately per-process, not per-machine.

        A persisted id would make a restarted client indistinguishable from a
        second copy running concurrently — which is the one distinction this
        exists to draw. A restart may take over from the connection its
        predecessor left behind; a concurrent duplicate must not. Reloading
        the module stands in for a fresh process.
        """
        import importlib

        first = identity.instance_id()
        try:
            reloaded = importlib.reload(identity)
            assert reloaded.instance_id() != first
        finally:
            importlib.reload(identity)


class TestHostname:
    def test_env_override_wins(self, monkeypatch):
        """Container hostnames are random hex; let the operator name the machine."""
        monkeypatch.setenv("HLE_HOSTNAME", "nas")
        assert identity.hostname() == "nas"

    def test_blank_override_falls_through_to_the_real_name(self, monkeypatch):
        monkeypatch.setenv("HLE_HOSTNAME", "   ")
        monkeypatch.setattr(socket, "gethostname", lambda: "mimos")
        assert identity.hostname() == "mimos"

    def test_is_bounded(self, monkeypatch):
        """It ends up in a close reason and a dashboard cell; cap it."""
        monkeypatch.delenv("HLE_HOSTNAME", raising=False)
        monkeypatch.setattr(socket, "gethostname", lambda: "h" * 500)
        assert len(identity.hostname()) == 128

    def test_an_unresolvable_hostname_is_not_an_error(self, monkeypatch):
        """The instance id is what distinguishes processes; this is only a label."""
        monkeypatch.delenv("HLE_HOSTNAME", raising=False)

        def _boom():
            raise OSError("no hostname")

        monkeypatch.setattr(socket, "gethostname", _boom)
        assert identity.hostname() is None

    def test_an_empty_hostname_is_none_rather_than_blank(self, monkeypatch):
        monkeypatch.delenv("HLE_HOSTNAME", raising=False)
        monkeypatch.setattr(socket, "gethostname", lambda: "  ")
        assert identity.hostname() is None


class TestLinuxInterfaceEnumeration:
    """Parsing real `ip -j addr` output.

    The fixture is captured verbatim from a Linux host running Docker: loopback,
    a LAN interface, `docker0`, a Docker user-network bridge, and a veth with
    only a link-local address. Firepuncher's default allowlist is built from
    this, so a parsing slip does not fail loudly — it silently narrows what the
    agent will forward to.
    """

    @staticmethod
    def _networks(monkeypatch) -> list[str]:
        from hle_client import netinfo

        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "ip_addr_linux.json"
        monkeypatch.setattr(netinfo, "_run", lambda argv: fixture.read_text())
        return [str(n) for n in netinfo._enumerate()]

    def test_the_lan_is_found(self, monkeypatch):
        """The whole point: an agent must be able to reach its own network."""
        assert "192.168.2.0/24" in self._networks(monkeypatch)

    def test_docker_networks_are_found(self, monkeypatch):
        """Both the default bridge and a user-defined one."""
        networks = self._networks(monkeypatch)
        assert "172.17.0.0/16" in networks
        assert "172.18.0.0/16" in networks

    def test_host_bits_are_discarded(self, monkeypatch):
        """`192.168.2.141/24` describes a host on a network, not the network."""
        networks = self._networks(monkeypatch)
        assert "192.168.2.141/24" not in networks
        assert "192.168.2.0/24" in networks

    def test_loopback_is_present(self, monkeypatch):
        assert "127.0.0.0/8" in self._networks(monkeypatch)

    def test_ipv6_link_local_is_kept(self, monkeypatch):
        """A v6 homelab addresses itself here; several interfaces share fe80::/64."""
        assert "fe80::/64" in self._networks(monkeypatch)

    def test_duplicates_across_interfaces_collapse(self, monkeypatch):
        """Three interfaces carry an fe80:: address; that is one network."""
        networks = self._networks(monkeypatch)
        assert networks.count("fe80::/64") == 1

    def test_a_lan_address_reads_as_local(self, monkeypatch):
        from hle_client import netinfo

        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "ip_addr_linux.json"
        monkeypatch.setattr(netinfo, "_run", lambda argv: fixture.read_text())
        netinfo.local_networks(refresh=True)
        try:
            assert netinfo.is_local("192.168.2.141")
            assert netinfo.is_local("172.17.0.5")
            assert not netinfo.is_local("93.184.216.34")
        finally:
            netinfo._cache = None

    def test_malformed_output_yields_loopback_rather_than_nothing(self, monkeypatch):
        """A parse failure must not leave an agent unable to reach even itself."""
        from hle_client import netinfo

        monkeypatch.setattr(netinfo, "_run", lambda argv: "not json")
        networks = [str(n) for n in netinfo._enumerate()]
        assert "127.0.0.0/8" in networks


class TestCloseCodes:
    @pytest.mark.parametrize(
        "code",
        [
            close_codes.INVALID_CREDENTIAL,
            close_codes.TUNNEL_LIMIT,
            close_codes.LABEL_IN_USE,
            close_codes.REPLACED,
            close_codes.DUPLICATE_INSTANCE,
        ],
    )
    def test_contested_and_rejected_codes_are_fatal(self, code):
        """Retrying any of these cannot help, and for the last two it harms."""
        assert close_codes.is_fatal(code)

    def test_a_flood_close_is_not_fatal(self):
        """ "Too fast" is a request to slow down; giving up would be worse."""
        assert not close_codes.is_fatal(close_codes.TOO_MANY_REGISTRATIONS)
        assert close_codes.retry_after_seconds(close_codes.TOO_MANY_REGISTRATIONS) == 60.0

    def test_an_ordinary_drop_is_neither(self):
        assert not close_codes.is_fatal(1006)
        assert close_codes.retry_after_seconds(1006) is None
        assert close_codes.retry_after_seconds(None) is None

    def test_no_code_is_used_twice(self):
        """Two meanings on one number is how a client comes to retry a stop."""
        codes = [
            close_codes.BAD_FIRST_MESSAGE,
            close_codes.INVALID_CREDENTIAL,
            close_codes.DISCONNECTED_BY_OWNER,
            close_codes.TUNNEL_LIMIT,
            close_codes.LABEL_IN_USE,
            close_codes.REPLACED,
            close_codes.DUPLICATE_INSTANCE,
            close_codes.TOO_MANY_REGISTRATIONS,
            close_codes.IDLE_TIMEOUT,
        ]
        assert len(codes) == len(set(codes))

    def test_all_codes_are_in_the_private_range(self):
        """RFC 6455 §7.4.2 — outside 4000-4999 a proxy may rewrite them."""
        for code in (
            close_codes.BAD_FIRST_MESSAGE,
            close_codes.INVALID_CREDENTIAL,
            close_codes.DISCONNECTED_BY_OWNER,
            close_codes.TUNNEL_LIMIT,
            close_codes.LABEL_IN_USE,
            close_codes.REPLACED,
            close_codes.DUPLICATE_INSTANCE,
            close_codes.TOO_MANY_REGISTRATIONS,
            close_codes.IDLE_TIMEOUT,
        ):
            assert 4000 <= code <= 4999
