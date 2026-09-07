"""Tests for the per-process client identity and the shared close codes."""

from __future__ import annotations

import socket

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
