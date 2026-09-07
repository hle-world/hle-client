"""Which networks this machine is actually attached to.

Firepuncher's default allowlist is "the networks the agent is on", and only the
agent can answer that. RFC 1918 was the obvious proxy and it is wrong in both
directions: it admits any Docker bridge while refusing Tailscale (100.64/10 is
CGNAT, not private) and refusing a homelab on native IPv6, whose addresses are
globally routable by design.

Reachability is not measured, deliberately. Traceroute and TTL games are the
tempting answer and a poor one for an authorization decision: ICMP is filtered
on most homelab gear, hop counts are forgeable, a probe on every connect costs
latency, and "one hop away" cannot tell a NAS from an ISP gateway. The
interface and route tables already hold the answer exactly, for free, and
update themselves as Docker, Kubernetes or a VPN come and go.

No third-party dependency: this runs on pfSense, in containers, and on hosts
where adding a wheel is not an option.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shutil
import subprocess  # nosemgrep: python-subprocess-usage
import time

logger = logging.getLogger(__name__)

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# How long any one enumeration command gets. These are local table reads that
# return in milliseconds; a timeout here means something is badly wrong and the
# caller is better served by the fallback than by waiting.
_COMMAND_TIMEOUT = 5.0


def _run(argv: list[str]) -> str | None:
    """Run a local enumeration command, or return None if it isn't usable."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603  # nosemgrep: python-subprocess-usage
            argv,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _network_of(addr: str, prefix: int | str) -> _IpNetwork | None:
    """The containing network for ``addr/prefix``, host bits discarded."""
    try:
        return ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    except ValueError:
        return None


def _from_ip_json() -> list[_IpNetwork]:
    """Linux with iproute2: ``ip -j addr`` gives every address and prefix."""
    out = _run(["ip", "-j", "addr"])
    if not out:
        return []
    try:
        interfaces = json.loads(out)
    except ValueError:
        return []
    networks: list[_IpNetwork] = []
    for iface in interfaces if isinstance(interfaces, list) else []:
        for entry in iface.get("addr_info", []) or []:
            local = entry.get("local")
            prefix = entry.get("prefixlen")
            if local and prefix is not None:
                net = _network_of(local, prefix)
                if net is not None:
                    networks.append(net)
    return networks


_INET_RE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)(?:/(\d+)|\s+netmask\s+(\S+))")
_INET6_RE = re.compile(r"\binet6\s+([0-9a-fA-F:]+)(?:%\w+)?(?:/|\s+prefixlen\s+)(\d+)")


def _from_ifconfig() -> list[_IpNetwork]:
    """macOS, FreeBSD and pfSense: parse ``ifconfig``.

    BSD prints the mask as a hex word (``netmask 0xffffff00``) where Linux
    prints a prefix length, so both spellings are handled.
    """
    out = _run(["ifconfig", "-a"]) or _run(["ifconfig"])
    if not out:
        return []
    networks: list[_IpNetwork] = []
    for match in _INET_RE.finditer(out):
        addr, prefix, netmask = match.groups()
        if prefix is not None:
            net = _network_of(addr, prefix)
        elif netmask is not None:
            try:
                bits = bin(int(netmask, 16)).count("1") if netmask.startswith("0x") else None
            except ValueError:
                bits = None
            net = _network_of(addr, bits) if bits is not None else None
        else:
            net = None
        if net is not None:
            networks.append(net)
    for match in _INET6_RE.finditer(out):
        net6 = _network_of(match.group(1), match.group(2))
        if net6 is not None:
            networks.append(net6)
    return networks


def _from_proc_route() -> list[_IpNetwork]:
    """Linux without iproute2: read the IPv4 route table out of /proc.

    Default routes are skipped. A default route is "everything else", which is
    the internet — expanding it here would turn the allowlist into no
    allowlist at all.
    """
    try:
        with open("/proc/net/route") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    networks: list[_IpNetwork] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            dest = int(fields[1], 16)
            mask = int(fields[7], 16)
        except ValueError:
            continue
        if dest == 0 and mask == 0:
            continue  # default route
        prefix = bin(mask).count("1")
        addr = ipaddress.IPv4Address(int.from_bytes(dest.to_bytes(4, "little"), "big"))
        net = _network_of(str(addr), prefix)
        if net is not None:
            networks.append(net)
    return networks


def _enumerate() -> list[_IpNetwork]:
    """Every network this machine is directly attached to.

    Loopback is always included — it is the agent's own machine, which is the
    one target that was never in doubt.

    A ``/32`` or ``/128`` is kept as itself rather than widened. Tailscale
    hands out exactly that, and its peers are not on a network this machine is
    attached to in any meaningful sense — widening to 100.64/10 would allow
    every address in the whole CGNAT range on the strength of one interface.
    Reaching a Tailscale peer is a rule away, and better said explicitly.
    """
    found: list[_IpNetwork] = []
    for source in (_from_ip_json, _from_ifconfig, _from_proc_route):
        try:
            found = source()
        except Exception:  # noqa: BLE001 — enumeration must never break a forward
            logger.debug("Interface enumeration via %s failed", source.__name__, exc_info=True)
            found = []
        if found:
            break

    networks: dict[str, _IpNetwork] = {}
    for net in found:
        # A /32 or /128 describes one address, not a network. Keep it — it is
        # still somewhere the agent legitimately sits — but it will only ever
        # match itself.
        networks[str(net)] = net

    for loopback in (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")):
        networks.setdefault(str(loopback), loopback)

    return sorted(networks.values(), key=str)


# Enumeration shells out, and this is consulted on every stream a forward
# opens — ssh alone opens several. Cached, with a short TTL so an interface
# coming up (a VPN connecting, Docker starting) is picked up without a restart.
_CACHE_TTL = 60.0
_cache: tuple[float, list[_IpNetwork]] | None = None


def local_networks(*, refresh: bool = False) -> list[_IpNetwork]:
    """Every network this machine is directly attached to, cached briefly."""
    global _cache
    now = time.monotonic()
    if not refresh and _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    networks = _enumerate()
    _cache = (now, networks)
    return networks


def describe_local_networks() -> list[str]:
    """``local_networks`` as strings, for showing a user what is allowed."""
    return [str(net) for net in local_networks()]


def is_local(host: str) -> bool:
    """Whether *host* is a literal address on one of this machine's networks.

    Names are never resolved here. The agent resolves them when it dials, so
    deciding on what a name resolves to *now* would check one answer and then
    use another.
    """
    try:
        address = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return False
    return any(address in net for net in local_networks() if net.version == address.version)
