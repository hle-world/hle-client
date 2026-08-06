"""Preflight checks — does this service actually work as a tunnel?

The case that motivated all of this: a pfSense box exposed as
``http://192.168.1.1`` reported healthy from every angle — agent online, upstream
answering in 29ms, SSO gate serving its login page — while every page load ended
at ``https://192.168.1.1/``, because the device answered 302 with its own address
and nothing rewrites that.

The redirect tests below run against a real local HTTP server that reproduces
exactly that behaviour, rather than a mocked response, because the value of this
feature is entirely in whether it recognises the real thing.
"""

from __future__ import annotations

import asyncio
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from hle_client.preflight import check_url_shape, run_preflight
from hle_common.preflight import Severity


def ids(report) -> list[str]:
    return [f.id for f in report.findings]


def by_id(report, finding_id):
    return next(f for f in report.findings if f.id == finding_id)


# --------------------------------------------------------------------------- #
# A local server that behaves like the upstreams this exists to catch
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    """Behaviour is chosen per-request by the server's ``mode``."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003 — silence the test server
        pass

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's interface
        mode = self.server.mode  # type: ignore[attr-defined]
        host = self.headers.get("Host", "")
        self.server.seen_hosts.append(host)  # type: ignore[attr-defined]

        if mode == "redirect_to_self_https":
            # pfSense: builds its redirect from the Host it was given.
            self._send(302, b"", {"Location": f"https://{host}/"})
        elif mode == "redirect_to_private_ip":
            self._send(302, b"", {"Location": "https://10.1.2.3/login"})
        elif mode == "redirect_relative":
            self._send(302, b"", {"Location": "/login"})
        elif mode == "rejects_tunnel_host":
            # Home Assistant / Django shape: fine for its own host, 400 otherwise.
            if "hle.world" in host:
                self._send(400, b"400: Bad Request")
            else:
                self._send(200, b"<html>ok</html>")
        elif mode == "pfsense_referer":
            self._send(
                403,
                b"An HTTP_REFERER was detected other than what is defined in System > Advanced",
            )
        elif mode == "https_on_http_port":
            self._send(400, b"400 Bad Request\nThe plain HTTP request was sent to HTTPS port")
        elif mode == "hsts_over_http":
            self._send(200, b"ok", {"Strict-Transport-Security": "max-age=31536000"})
        elif mode == "cookie_elsewhere":
            self._send(200, b"ok", {"Set-Cookie": "SID=abc; Domain=192.168.1.1; Path=/"})
        elif mode == "needs_auth":
            self._send(401, b"nope", {"WWW-Authenticate": 'Basic realm="router"'})
        elif mode == "private_links":
            body = b"<html><img src='http://192.168.1.1/logo.png'><a href='http://192.168.1.1/x'>"
            self._send(200, body)
        else:
            self._send(200, b"<html>ok</html>")

    def _send(self, status, body, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@pytest.fixture(scope="module")
def _server():
    """One server for the module: shutdown costs ~0.5s of poll interval each."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.mode = "ok"
    server.seen_hosts = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def upstream(_server):
    """A local HTTP server whose behaviour each test selects."""
    _server.mode = "ok"
    _server.seen_hosts.clear()
    return _server


def url_for(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def run(server, **kwargs):
    return asyncio.run(run_preflight(url_for(server), **kwargs))


# --------------------------------------------------------------------------- #
# No network needed
# --------------------------------------------------------------------------- #
class TestUrlShape:
    def test_missing_scheme_is_fatal_and_offers_one(self):
        (finding,) = check_url_shape("192.168.1.1:8006")
        assert finding.id == "url_missing_scheme"
        assert finding.severity == Severity.ERROR
        assert finding.fix.value == "http://192.168.1.1:8006"

    def test_trailing_slash_is_flagged_with_the_trimmed_url(self):
        """The double-slash bug that broke Proxmox consoles."""
        (finding,) = check_url_shape("https://192.168.2.200:8006/")
        assert finding.id == "url_trailing_slash"
        assert finding.fix.value == "https://192.168.2.200:8006"

    def test_a_bare_url_is_clean(self):
        assert check_url_shape("http://192.168.1.50:8096") == []

    def test_non_http_scheme_is_rejected(self):
        (finding,) = check_url_shape("ssh://192.168.1.1:22")
        assert finding.id == "url_unsupported_scheme"
        assert finding.severity == Severity.ERROR

    def test_embedded_credentials_are_flagged_without_echoing_them(self):
        (finding,) = check_url_shape("http://admin:hunter2@192.168.1.1")
        assert finding.id == "url_embedded_credentials"
        assert "hunter2" not in finding.evidence

    def test_a_fatal_shape_problem_skips_the_network_entirely(self):
        """No point probing a URL already known to be unusable."""
        report = asyncio.run(run_preflight("not a url at all"))
        assert ids(report) == ["url_missing_scheme"]


# --------------------------------------------------------------------------- #
# The bug this feature exists for
# --------------------------------------------------------------------------- #
class TestRedirects:
    def test_redirect_to_its_own_https_offers_the_scheme_switch(self, upstream):
        """Exactly the gw-ian case."""
        upstream.mode = "redirect_to_self_https"
        report = run(upstream)
        finding = by_id(report, "upstream_redirects_to_https")
        assert finding.severity == Severity.ERROR
        assert finding.fix.field == "service_url"
        assert finding.fix.value.startswith("https://127.0.0.1:")
        assert "302" in finding.evidence

    def test_redirect_to_an_unrelated_private_address(self, upstream):
        upstream.mode = "redirect_to_private_ip"
        report = run(upstream)
        finding = by_id(report, "upstream_redirects_to_private_address")
        assert finding.severity == Severity.ERROR
        assert "10.1.2.3" in finding.evidence

    def test_a_private_redirect_suggests_forwarding_the_host(self, upstream):
        """It built that URL from the Host we sent, so try sending the real one."""
        upstream.mode = "redirect_to_private_ip"
        report = run(upstream)
        assert by_id(report, "upstream_redirects_to_private_address").fix.field == "forward_host"

    def test_a_relative_redirect_is_not_a_problem(self, upstream):
        """It stays on the tunnel host, so it works."""
        upstream.mode = "redirect_relative"
        report = run(upstream)
        assert not [i for i in ids(report) if "redirect" in i]


class TestHostHandling:
    def test_the_two_host_modes_are_both_probed(self, upstream):
        """The whole design rests on comparing them."""
        run(upstream, tunnel_host="gw-ian.hle.world")
        assert any("hle.world" in h for h in upstream.seen_hosts)
        assert any("127.0.0.1" in h for h in upstream.seen_hosts)

    def test_host_rejection_is_only_informational_by_default(self, upstream):
        """The proxy strips Host by default, so this doesn't bite unless asked."""
        upstream.mode = "rejects_tunnel_host"
        report = run(upstream, tunnel_host="gw-ian.hle.world")
        assert by_id(report, "upstream_rejects_tunnel_host").severity == Severity.INFO

    def test_host_rejection_is_fatal_when_forwarding_is_on(self, upstream):
        upstream.mode = "rejects_tunnel_host"
        report = run(upstream, tunnel_host="gw-ian.hle.world", forward_host=True)
        assert by_id(report, "upstream_rejects_tunnel_host").severity == Severity.ERROR
        assert by_id(report, "forward_host_breaks_this_upstream").fix.value == "false"

    def test_pfsense_referer_rejection_is_named_precisely(self, upstream):
        upstream.mode = "pfsense_referer"
        report = run(upstream, tunnel_host="gw-ian.hle.world")
        finding = by_id(report, "upstream_checks_referer")
        assert "Alternate Hostnames" in finding.detail

    def test_a_healthy_service_reports_which_mode_worked(self, upstream):
        report = run(upstream, tunnel_host="gw-ian.hle.world")
        assert report.working_host_mode == "upstream"
        assert not [f for f in report.findings if f.severity == Severity.ERROR]


class TestSchemeMismatch:
    def test_plaintext_to_an_https_port_offers_https(self, upstream):
        upstream.mode = "https_on_http_port"
        report = run(upstream)
        finding = by_id(report, "upstream_wants_https")
        assert finding.severity == Severity.ERROR
        assert finding.fix.value.startswith("https://")

    def test_nothing_listening_is_reported_as_refused(self):
        report = asyncio.run(run_preflight("http://127.0.0.1:9"))
        finding = by_id(report, "upstream_unreachable")
        assert finding.severity == Severity.ERROR
        assert finding.evidence  # never an empty explanation


class TestSofterProblems:
    def test_hsts_over_plaintext_warns(self, upstream):
        upstream.mode = "hsts_over_http"
        report = run(upstream)
        assert by_id(report, "upstream_sends_hsts_over_http").severity == Severity.WARNING

    def test_a_cookie_scoped_elsewhere_warns(self, upstream):
        upstream.mode = "cookie_elsewhere"
        report = run(upstream, tunnel_host="gw-ian.hle.world")
        finding = by_id(report, "upstream_cookie_scoped_elsewhere")
        assert "192.168.1.1" in finding.detail
        # The cookie value must not be echoed back across the wire.
        assert "abc" not in finding.evidence

    def test_auth_required_is_information_not_a_fault(self, upstream):
        upstream.mode = "needs_auth"
        report = run(upstream)
        assert by_id(report, "upstream_requires_auth").severity == Severity.INFO

    def test_absolute_private_links_in_the_page_warn(self, upstream):
        upstream.mode = "private_links"
        report = run(upstream)
        finding = by_id(report, "page_references_private_address")
        assert finding.fix.field == "forward_host"

    def test_a_clean_service_produces_nothing(self, upstream):
        report = run(upstream)
        assert report.findings == []
        assert report.error is None


class TestReportContract:
    def test_the_request_id_comes_back_for_correlation(self, upstream):
        report = run(upstream, request_id="abc-123")
        assert report.request_id == "abc-123"

    def test_could_not_check_is_distinct_from_nothing_wrong(self):
        """Silence must never read as a pass."""
        report = asyncio.run(run_preflight("http://127.0.0.1:9"))
        assert report.findings  # a refusal is a finding, not an empty pass

    def test_the_report_round_trips_over_the_wire(self, upstream):
        upstream.mode = "redirect_to_self_https"
        from hle_common.preflight import PreflightReport

        report = run(upstream)
        again = PreflightReport.model_validate_json(report.model_dump_json())
        assert ids(again) == ids(report)
        assert again.findings[0].fix.value == report.findings[0].fix.value

    def test_evidence_is_always_bounded(self, upstream):
        """It crosses the wire and lands in a browser."""
        upstream.mode = "private_links"
        report = run(upstream)
        assert all(len(f.evidence) <= 301 for f in report.findings)

    def test_elapsed_is_recorded(self, upstream):
        report = run(upstream)
        assert report.elapsed_ms >= 0


class TestWebsockets:
    def test_no_websocket_probe_when_websockets_are_enabled(self, upstream):
        """Nothing to warn about, so don't spend a request on it."""
        run(upstream, websocket_enabled=True)
        baseline = len(upstream.seen_hosts)
        upstream.seen_hosts.clear()
        run(upstream, websocket_enabled=False)
        assert len(upstream.seen_hosts) > baseline


def test_private_host_detection():
    from hle_client.preflight import _is_private_host

    assert _is_private_host("192.168.1.1")
    assert _is_private_host("10.0.0.5")
    assert _is_private_host("172.16.0.1")
    assert _is_private_host("127.0.0.1")
    assert _is_private_host("pfsense")  # a bare name only resolves locally
    assert not _is_private_host("hle.world")
    assert not _is_private_host("8.8.8.8")
    assert not _is_private_host("")


def test_no_finding_id_is_duplicated():
    """Findings are keyed by id in the UI, so collisions would hide one."""
    source = (
        __import__("pathlib").Path(__import__("hle_client.preflight", fromlist=["x"]).__file__)
    ).read_text()
    found = re.findall(r'id="([a-z_]+)"', source)
    assert len(found) == len(set(found)), [i for i in found if found.count(i) > 1]
