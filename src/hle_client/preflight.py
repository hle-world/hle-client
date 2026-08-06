"""Preflight checks — probe a service the way a tunnel would, and report.

Runs on the machine that can actually reach the service, which is the whole
point: the server cannot tell you anything about ``http://192.168.1.1``.

The design rule is that a check must produce **evidence**, not a guess. Every
finding names what was sent and what came back, because the failures this exists
to catch are exactly the ones where every simple signal says the service is fine.

Two host modes are probed, not one. The proxy strips the browser's ``Host`` by
default (``proxy.py``), so the upstream normally sees its own address — which is
why a router answers a redirect pointing at its LAN IP, and why an app that
builds absolute URLs emits unreachable ones. Forwarding the real host fixes that
but trips the host and referer validation in pfSense, Home Assistant, Django and
Grafana. Neither mode is right in general, so preflight tries both and reports
which one works rather than assuming.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from hle_common.preflight import PreflightFinding, PreflightFix, PreflightReport, Severity

logger = logging.getLogger(__name__)

# One request. Short: these are LAN addresses, and a service that needs longer
# than this to answer its root path will feel broken through a tunnel anyway.
REQUEST_TIMEOUT = 4.0
# The whole run, so a preflight can never outlive the caller's patience or hold
# an agent's control channel. Checks already completed are still reported.
TOTAL_BUDGET = 20.0
# Bodies are sniffed for known error signatures. Bounded because this crosses
# the wire into a browser.
BODY_SNIFF_BYTES = 65536
EVIDENCE_CHARS = 300

# Signatures that identify *why* a 4xx happened. Matching prose is unpleasant but
# it is the only thing these devices give us, and being specific is what turns
# "400 Bad Request" into an actionable finding.
_HTTPS_ON_HTTP_PORT = re.compile(r"plain HTTP request was sent to HTTPS port", re.I)
_PFSENSE_REFERER = re.compile(r"HTTP_REFERER was detected other than what is defined", re.I)
_HOST_REJECTED = re.compile(
    r"invalid host|invalid http_host|host header|400: Bad Request|requested host name",
    re.I,
)


@dataclass
class _Probe:
    """One request's outcome, or the exception that replaced it."""

    response: httpx.Response | None = None
    error: BaseException | None = None
    body: str = ""

    @property
    def ok(self) -> bool:
        return self.response is not None and self.response.status_code < 400

    @property
    def status(self) -> int | None:
        return self.response.status_code if self.response is not None else None


def _is_private_host(host: str) -> bool:
    """Whether a hostname is an address only reachable on the local network."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # A bare name with no dots ("pfsense") only resolves on a local network.
        return "." not in host and host != "localhost"
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


_ABSOLUTE_URL_HOST = re.compile(r"https?://([A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])(?::\d+)?")


def _private_hosts_in(body: str) -> list[str]:
    """Private hosts referenced by absolute URLs in a page.

    Any of them, not just the upstream's own address: a dashboard that links to
    other machines on the LAN is just as broken through a tunnel as one that
    links to itself.
    """
    hosts = {m.group(1) for m in _ABSOLUTE_URL_HOST.finditer(body)}
    return sorted(h for h in hosts if _is_private_host(h))


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EVIDENCE_CHARS else text[: EVIDENCE_CHARS - 1] + "…"


async def _probe(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None
) -> _Probe:
    """GET once, never raising. GET rather than HEAD deliberately.

    ``service.check`` used HEAD and fell back to GET only on a transport error,
    so a device that rejects HEAD reported a 400 that looked like a real fault.
    """
    try:
        resp = await asyncio.wait_for(
            client.get(url, headers=headers or {}), timeout=REQUEST_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
        return _Probe(error=exc)
    body = ""
    try:
        raw = resp.content[:BODY_SNIFF_BYTES]
        body = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — a body we can't read is not a failure
        body = ""
    return _Probe(response=resp, body=body)


def check_url_shape(service_url: str) -> list[PreflightFinding]:
    """Checks that need no network at all."""
    out: list[PreflightFinding] = []
    parsed = urlparse(service_url)

    if not parsed.scheme:
        out.append(
            PreflightFinding(
                id="url_missing_scheme",
                severity=Severity.ERROR,
                title="The service URL has no scheme",
                detail="Give the URL as http://host:port or https://host:port.",
                evidence=_clip(service_url),
                fix=PreflightFix(
                    field="service_url",
                    value=f"http://{service_url}",
                    label=f"Use http://{service_url}",
                ),
            )
        )
        return out  # everything below assumes a parseable URL

    if parsed.scheme not in ("http", "https"):
        out.append(
            PreflightFinding(
                id="url_unsupported_scheme",
                severity=Severity.ERROR,
                title=f"{parsed.scheme}:// is not a supported service URL",
                detail="A tunnel proxies HTTP and WebSocket. Use http:// or https://.",
                evidence=_clip(service_url),
            )
        )
        return out

    # Includes the bare "http://host:8006/" case, which is the one that actually
    # bit: paths are appended, so "/" + "/api2" became "//api2" and Proxmox
    # consoles returned HTTP 500.
    if parsed.path.endswith("/"):
        trimmed = urlunparse(parsed._replace(path=parsed.path.rstrip("/")))
        out.append(
            PreflightFinding(
                id="url_trailing_slash",
                severity=Severity.WARNING,
                title="The service URL ends in a slash",
                detail=(
                    "Paths are appended to this URL, so a trailing slash produces "
                    "a double slash upstream. That returned HTTP 500 from Proxmox "
                    "consoles until it was fixed, and it still makes WebSocket "
                    "URLs wrong on some upstreams."
                ),
                evidence=_clip(service_url),
                fix=PreflightFix(field="service_url", value=trimmed, label=f"Use {trimmed}"),
            )
        )

    if parsed.username or parsed.password:
        out.append(
            PreflightFinding(
                id="url_embedded_credentials",
                severity=Severity.WARNING,
                title="The service URL contains credentials",
                detail=(
                    "Credentials in the URL are stored with the tunnel and appear "
                    "in logs. Use the upstream basic-auth setting instead."
                ),
                evidence=_clip(urlunparse(parsed._replace(netloc=parsed.hostname or ""))),
            )
        )

    return out


def _redirect_finding(probe: _Probe, service_url: str, host_mode: str) -> PreflightFinding | None:
    """A redirect that sends the browser somewhere it cannot follow."""
    resp = probe.response
    if resp is None or not (300 <= resp.status_code < 400):
        return None
    location = resp.headers.get("location", "")
    if not location:
        return None
    target = urlparse(location)
    if not target.netloc:
        return None  # relative redirect: fine, it stays on the tunnel host

    upstream = urlparse(service_url)
    same_host = target.hostname == upstream.hostname
    if not _is_private_host(target.hostname or ""):
        return None

    # http -> https on the same host is the one case with a clean, obvious fix.
    if same_host and target.scheme == "https" and upstream.scheme == "http":
        https_url = urlunparse(upstream._replace(scheme="https"))
        return PreflightFinding(
            id="upstream_redirects_to_https",
            severity=Severity.ERROR,
            title="The service redirects to HTTPS on its own address",
            detail=(
                "The browser would leave the tunnel for a private address it "
                "cannot reach. Pointing the tunnel at https:// removes the "
                "redirect. The upstream certificate is not verified, so a "
                "self-signed one is fine."
            ),
            evidence=_clip(f"{resp.status_code} location: {location}"),
            fix=PreflightFix(field="service_url", value=https_url, label=f"Use {https_url}"),
        )

    finding = PreflightFinding(
        id="upstream_redirects_to_private_address",
        severity=Severity.ERROR,
        title="The service redirects to a private address",
        detail=(
            "The browser would be sent off the tunnel to an address only "
            "reachable on your network. Nothing rewrites this redirect."
        ),
        evidence=_clip(f"{resp.status_code} location: {location}"),
    )
    if host_mode == "upstream":
        # It built that URL from the Host we gave it, which was its own address.
        finding.detail += (
            " It built that URL from the Host header it was sent, which is its "
            "own address by default — forwarding the tunnel's hostname instead "
            "may make it redirect correctly."
        )
        finding.fix = PreflightFix(
            field="forward_host", value="true", label="Forward the tunnel hostname"
        )
    return finding


def _scheme_findings(probe: _Probe, service_url: str) -> list[PreflightFinding]:
    """Wrong scheme, in either direction."""
    out: list[PreflightFinding] = []
    parsed = urlparse(service_url)

    if probe.response is not None and _HTTPS_ON_HTTP_PORT.search(probe.body):
        https_url = urlunparse(parsed._replace(scheme="https"))
        out.append(
            PreflightFinding(
                id="upstream_wants_https",
                severity=Severity.ERROR,
                title="The service speaks HTTPS on this port",
                detail="It rejected a plaintext request outright.",
                evidence=_clip(f"{probe.status} {probe.body}"),
                fix=PreflightFix(field="service_url", value=https_url, label=f"Use {https_url}"),
            )
        )
        return out

    err = probe.error
    if err is None:
        return out

    name = type(err).__name__
    text = str(err)
    if parsed.scheme == "https" and ("SSLError" in name or "record layer" in text.lower()):
        http_url = urlunparse(parsed._replace(scheme="http"))
        out.append(
            PreflightFinding(
                id="upstream_is_plaintext",
                severity=Severity.ERROR,
                title="The service is not speaking TLS on this port",
                detail="A TLS handshake failed against it. It is probably plain HTTP.",
                evidence=_clip(f"{name}: {text}"),
                fix=PreflightFix(field="service_url", value=http_url, label=f"Use {http_url}"),
            )
        )
    elif "CertificateError" in name or "CERTIFICATE_VERIFY_FAILED" in text:
        out.append(
            PreflightFinding(
                id="upstream_certificate_not_trusted",
                severity=Severity.ERROR,
                title="The upstream certificate could not be verified",
                detail=(
                    "Certificate verification is on for this tunnel. Homelab "
                    "services normally use self-signed certificates, and HLE "
                    "terminates TLS at the relay regardless."
                ),
                evidence=_clip(f"{name}: {text}"),
                fix=PreflightFix(
                    field="verify_ssl", value="false", label="Stop verifying the upstream cert"
                ),
            )
        )
    elif "ConnectError" in name or "ConnectTimeout" in name or "TimeoutError" in name:
        timed_out = "Timeout" in name or "TimeoutError" in name
        out.append(
            PreflightFinding(
                id="upstream_unreachable",
                severity=Severity.ERROR,
                title=(
                    f"The service did not answer within {REQUEST_TIMEOUT:.0f}s"
                    if timed_out
                    else "The service refused the connection"
                ),
                detail=(
                    "Nothing accepted a connection at this address from the "
                    "machine running the agent. Check the address and port, and "
                    "that the service is not bound to localhost on a different "
                    "host."
                ),
                # Timeouts often carry an empty message, which reads as though
                # nothing was learned.
                evidence=_clip(
                    f"{name}: {text}" if text else f"{name} after {REQUEST_TIMEOUT:.0f}s"
                ),
            )
        )
    else:
        out.append(
            PreflightFinding(
                id="upstream_request_failed",
                severity=Severity.ERROR,
                title="The request to the service failed",
                evidence=_clip(f"{name}: {text}"),
            )
        )
    return out


def _host_findings(
    upstream: _Probe, tunnel: _Probe, tunnel_host: str, forward_host: bool
) -> list[PreflightFinding]:
    """What the two host modes tell us together."""
    out: list[PreflightFinding] = []

    rejects_tunnel_host = (
        tunnel.response is not None
        and tunnel.status in (400, 403, 421)
        and (_HOST_REJECTED.search(tunnel.body) or _PFSENSE_REFERER.search(tunnel.body))
    )

    if _PFSENSE_REFERER.search(tunnel.body):
        out.append(
            PreflightFinding(
                id="upstream_checks_referer",
                severity=Severity.ERROR if forward_host else Severity.INFO,
                title="The service validates the hostname it is reached by",
                detail=(
                    "pfSense and OPNsense reject requests whose Host or Referer "
                    "is not one of their known hostnames. Add the tunnel "
                    "hostname under System → Advanced → Alternate Hostnames, "
                    "which the pfSense helper script does for you."
                ),
                evidence=_clip(f"Host: {tunnel_host} -> {tunnel.status} {tunnel.body}"),
            )
        )
    elif rejects_tunnel_host:
        out.append(
            PreflightFinding(
                id="upstream_rejects_tunnel_host",
                severity=Severity.ERROR if forward_host else Severity.INFO,
                title="The service rejects the tunnel's hostname",
                detail=(
                    "Apps that validate Host — Home Assistant, Django, Grafana, "
                    "UniFi — need the tunnel hostname added to their allow-list. "
                    "By default HLE sends the service its own address instead, "
                    "so this only bites if host forwarding is on."
                ),
                evidence=_clip(f"Host: {tunnel_host} -> {tunnel.status} {tunnel.body}"),
            )
        )

    if forward_host and rejects_tunnel_host and upstream.ok:
        out.append(
            PreflightFinding(
                id="forward_host_breaks_this_upstream",
                severity=Severity.ERROR,
                title="Host forwarding is on and this service refuses it",
                detail="It answered normally when sent its own address.",
                evidence=_clip(f"tunnel host -> {tunnel.status}, own host -> {upstream.status}"),
                fix=PreflightFix(
                    field="forward_host", value="false", label="Stop forwarding the hostname"
                ),
            )
        )
    return out


def _content_findings(
    probe: _Probe, service_url: str, tunnel_host: str | None
) -> list[PreflightFinding]:
    """Softer problems visible in a successful response."""
    out: list[PreflightFinding] = []
    resp = probe.response
    if resp is None:
        return out

    if resp.status_code in (401, 403):
        out.append(
            PreflightFinding(
                id="upstream_requires_auth",
                severity=Severity.INFO,
                title="The service asks for its own login",
                detail=(
                    "Not a fault. With SSO in front, people will sign in twice — "
                    "once to HLE, once to the service."
                ),
                evidence=_clip(f"{resp.status_code} {resp.headers.get('www-authenticate', '')}"),
            )
        )

    if urlparse(service_url).scheme == "http" and "strict-transport-security" in resp.headers:
        out.append(
            PreflightFinding(
                id="upstream_sends_hsts_over_http",
                severity=Severity.WARNING,
                title="The service sends HSTS over plaintext",
                detail=(
                    "Browsers will remember to force HTTPS for the tunnel "
                    "hostname, which can strand other tunnels on the same domain."
                ),
                evidence=_clip(resp.headers.get("strict-transport-security", "")),
            )
        )

    for cookie in resp.headers.get_list("set-cookie"):
        match = re.search(r"domain=([^;]+)", cookie, re.I)
        if match:
            domain = match.group(1).strip().lstrip(".")
            if tunnel_host and domain not in tunnel_host:
                out.append(
                    PreflightFinding(
                        id="upstream_cookie_scoped_elsewhere",
                        severity=Severity.WARNING,
                        title="A cookie is scoped to another domain",
                        detail=(
                            f"The browser will not send a cookie scoped to "
                            f"{domain} to {tunnel_host}, so signing in will not "
                            f"stick."
                        ),
                        evidence=_clip(cookie.split("=", 1)[0] + f"; Domain={domain}"),
                    )
                )
                break

    private_linked = _private_hosts_in(probe.body)
    if private_linked:
        hits = sum(probe.body.count(h) for h in private_linked)
        out.append(
            PreflightFinding(
                id="page_references_private_address",
                severity=Severity.WARNING,
                title="The page contains links to a private address",
                detail=(
                    "Absolute URLs pointing at the service's own address will "
                    "not load through the tunnel. Forwarding the tunnel hostname "
                    "often makes the service emit correct URLs instead."
                ),
                evidence=_clip(f"{hits} reference(s) to {', '.join(private_linked)}"),
                fix=PreflightFix(
                    field="forward_host", value="true", label="Forward the tunnel hostname"
                ),
            )
        )

    return out


async def _websocket_finding(
    client: httpx.AsyncClient, url: str, websocket_enabled: bool
) -> PreflightFinding | None:
    """Does this service want WebSockets it will not be allowed to use?"""
    if websocket_enabled:
        return None
    probe = await _probe(
        client,
        url,
        headers={
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": "cHJlZmxpZ2h0LXByb2JlLTEyMw==",
        },
    )
    if probe.response is not None and probe.response.status_code == 101:
        return PreflightFinding(
            id="upstream_wants_websockets",
            severity=Severity.ERROR,
            title="The service uses WebSockets and they are disabled",
            detail=(
                "It accepted a WebSocket upgrade. Consoles, live logs and "
                "dashboards will hang with WebSocket support off."
            ),
            evidence="101 Switching Protocols",
            fix=PreflightFix(
                field="websocket_enabled", value="true", label="Enable WebSocket support"
            ),
        )
    return None


async def run_preflight(
    service_url: str,
    *,
    tunnel_host: str | None = None,
    verify_ssl: bool = False,
    websocket_enabled: bool = True,
    forward_host: bool = False,
    request_id: str = "",
) -> PreflightReport:
    """Probe ``service_url`` as this tunnel would, and report what is wrong.

    Never raises and never changes anything. A check that fails to run is a
    reported error, not an exception: "could not check" and "nothing wrong" must
    not look the same.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    shape = check_url_shape(service_url)
    if any(f.severity == Severity.ERROR for f in shape):
        # No point probing a URL we already know is unusable.
        return PreflightReport(
            request_id=request_id,
            service_url=service_url,
            findings=shape,
            elapsed_ms=round((loop.time() - started) * 1000, 1),
        )

    findings = list(shape)
    working_mode: str | None = None
    error: str | None = None

    try:
        async with asyncio.timeout(TOTAL_BUDGET):
            async with httpx.AsyncClient(
                verify=verify_ssl,
                follow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as client:
                # Mode 1: Host stripped — what the proxy does by default, so the
                # upstream sees its own address.
                upstream = await _probe(client, service_url)
                findings += _scheme_findings(upstream, service_url)

                if any(f.severity == Severity.ERROR for f in findings):
                    return PreflightReport(
                        request_id=request_id,
                        service_url=service_url,
                        findings=findings,
                        elapsed_ms=round((loop.time() - started) * 1000, 1),
                    )

                redirect = _redirect_finding(upstream, service_url, "upstream")
                if redirect is not None:
                    findings.append(redirect)
                elif upstream.ok:
                    working_mode = "upstream"

                # Mode 2: Host forwarded — what an app needs to build correct
                # URLs, and what host-validating services reject.
                if tunnel_host:
                    tunnel = await _probe(client, service_url, headers={"Host": tunnel_host})
                    findings += _host_findings(upstream, tunnel, tunnel_host, forward_host)
                    tunnel_redirect = _redirect_finding(tunnel, service_url, "tunnel")
                    if tunnel_redirect is None and tunnel.ok and working_mode is None:
                        working_mode = "tunnel"

                findings += _content_findings(upstream, service_url, tunnel_host)
                ws = await _websocket_finding(client, service_url, websocket_enabled)
                if ws is not None:
                    findings.append(ws)
    except TimeoutError:
        error = f"Checks did not finish within {TOTAL_BUDGET:.0f}s"
    except Exception as exc:  # noqa: BLE001 — reporting beats crashing an agent
        logger.debug("Preflight failed for %s: %s", service_url, exc)
        error = f"{type(exc).__name__}: {exc}"[:200]

    return PreflightReport(
        request_id=request_id,
        service_url=service_url,
        findings=findings,
        error=error,
        elapsed_ms=round((loop.time() - started) * 1000, 1),
        working_host_mode=working_mode,
    )
