"""``hle fp`` — firepuncher: forward a remote agent's TCP port to a local one.

    hle fp --agent rpi --to localhost:22 --port 9922
    ssh -p 9922 root@localhost

The connection is private end to end: this client authenticates to the relay
with your API key, the relay pairs it to your agent, and the agent only dials
targets on its allowlist. Nothing listens on a public port.
"""

from __future__ import annotations

import asyncio
import json
import logging

import click
import websockets
from rich.console import Console

from hle_client import __version__
from hle_client.firepuncher import FpLocalClient
from hle_client.tunnel import _load_api_key
from hle_common.fp_protocol import FpHello, FpWelcome

logger = logging.getLogger(__name__)
console = Console()

WS_MAX_MESSAGE_SIZE = 4 * 1024 * 1024


def parse_target(target: str) -> tuple[str, int]:
    """Parse ``host:port``; a bare port means loopback on the agent.

    ``22`` -> ``("localhost", 22)``, ``nas:8096`` -> ``("nas", 8096)``.
    IPv6 literals must be bracketed: ``[::1]:22``.
    """
    raw = target.strip()
    if raw.isdigit():
        return "localhost", int(raw)

    if raw.startswith("["):  # [::1]:22
        host, _, port = raw.rpartition("]:")
        if not port:
            raise click.BadParameter(f"Expected [host]:port, got {target!r}")
        return host.lstrip("["), _parse_port(port, target)

    host, sep, port = raw.rpartition(":")
    if not sep or not host:
        raise click.BadParameter(
            f"Expected host:port or a bare port, got {target!r} (e.g. --to localhost:22 or --to 22)"
        )
    return host, _parse_port(port, target)


def _parse_port(value: str, original: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise click.BadParameter(f"Port must be a number in {original!r}") from None
    if not 1 <= port <= 65535:
        raise click.BadParameter(f"Port out of range in {original!r}")
    return port


def fp_uri(relay_host: str, relay_port: int) -> str:
    scheme = "ws" if relay_host.startswith("localhost") else "wss"
    return f"{scheme}://{relay_host}:{relay_port}/_hle/fp"


# Close codes that mean "stop trying": the problem is the credential or the
# request, and retrying just repeats it. Anything else — relay restart, network
# drop, agent temporarily offline — is worth reconnecting through.
_FATAL_CLOSE_CODES = frozenset({4001, 4003})

RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0

# "The relay is fine, your agent just isn't there yet" is a different situation
# from "I can't reach the relay". The relay answered, so polling it is cheap,
# and the thing we're waiting for (an agent reconnecting) typically returns in
# seconds. Backing this off to MAX_RECONNECT_DELAY meant the forward stayed
# dead for up to 30s after the agent was already available again.
AGENT_WAIT_DELAY = 2.0
MAX_AGENT_WAIT_DELAY = 5.0


async def _run(
    *,
    api_key: str,
    agent: str,
    target_host: str,
    target_port: int,
    bind_host: str,
    bind_port: int,
    relay_host: str,
    relay_port: int,
) -> None:
    """Serve the local port, reconnecting to the relay as needed.

    The listener is bound once and kept for the lifetime of the command, so a
    relay restart or a dropped network doesn't take the local port away. Streams
    in flight when the connection drops cannot be recovered — the far end is
    gone — so they're closed and the caller reconnects, but the port stays
    there ready for them.
    """
    uri = fp_uri(relay_host, relay_port)
    client = FpLocalClient(
        send=_not_connected,
        target_host=target_host,
        target_port=target_port,
        on_error=lambda detail: console.print(f"[red]Refused:[/red] {detail}"),
    )
    server = await client.serve(bind_host, bind_port)
    printed_banner = False
    delay = RECONNECT_DELAY
    agent_wait = AGENT_WAIT_DELAY
    # Repeating an identical line every couple of seconds reads like a fault
    # even when the retry is working exactly as intended. Say it once, then say
    # it again only when the situation actually changes.
    last_notice: str | None = None
    last_retry_delay: float | None = None

    def notice(message: str, *, style: str = "yellow") -> None:
        nonlocal last_notice
        if message != last_notice:
            console.print(f"[{style}]{message}[/{style}]")
            last_notice = message

    async with server:
        while True:
            try:
                async with websockets.connect(uri, max_size=WS_MAX_MESSAGE_SIZE) as ws:
                    await ws.send(
                        FpHello(
                            api_key=api_key, agent=agent, client_version=__version__
                        ).model_dump_json()
                    )
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    first = json.loads(raw)
                    if first.get("type") == "fp_error":
                        detail = first.get("message") or first.get("code")
                        code = first.get("code")
                        if code == "agent_offline":
                            # The relay is reachable — only the agent is missing,
                            # and it is probably reconnecting. Poll on its own
                            # short schedule rather than the connection backoff.
                            notice(f"{detail} Waiting for it to come back...")
                            await asyncio.sleep(agent_wait)
                            agent_wait = min(agent_wait * 2, MAX_AGENT_WAIT_DELAY)
                            # Reaching the relay at all means the network is
                            # healthy, so don't carry a stale connection backoff.
                            delay = RECONNECT_DELAY
                            continue
                        console.print(f"[red]Error:[/red] {detail}")
                        raise SystemExit(1)

                    welcome = FpWelcome.model_validate(first)
                    client.send = ws.send
                    client.connected = True
                    delay = RECONNECT_DELAY
                    agent_wait = AGENT_WAIT_DELAY

                    if not printed_banner:
                        console.print(
                            f"[green]Forwarding[/green] {bind_host}:{bind_port} "
                            f"→ [cyan]{agent}[/cyan] → {target_host}:{target_port}"
                        )
                        if welcome.allowed:
                            console.print(f"[dim]Agent allows: {', '.join(welcome.allowed)}[/dim]")
                        if target_port == 22:
                            console.print(f"[dim]Try: ssh -p {bind_port} <user>@{bind_host}[/dim]")
                        console.print("[dim]Ctrl+C to stop.[/dim]")
                        printed_banner = True
                    else:
                        console.print("[green]Reconnected.[/green] Forward is live again.")
                    last_notice = None
                    last_retry_delay = None

                    await _read_loop(ws, client)

                # A clean close still means the session ended; reconnect.
                notice("Connection closed by the relay")

            except SystemExit:
                raise
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code in _FATAL_CLOSE_CODES:
                    console.print(f"[red]Error:[/red] {exc.rcvd.reason or 'rejected by relay'}")
                    raise SystemExit(1) from None
                notice(f"Connection lost ({_close_reason(exc)})")
            except OSError as exc:
                # DNS failure, no route, refused — typical when the laptop's
                # network drops entirely.
                notice(f"Cannot reach the relay ({exc})")
            except Exception as exc:  # noqa: BLE001 — never surface a traceback here
                notice(f"Connection problem ({exc})")
            finally:
                client.connected = False
                client.send = _not_connected
                # Streams cannot outlive the connection that carried them.
                await client.close_all()

            # Once this reaches the ceiling it stops changing, so printing it
            # every pass would just be the same line forever.
            if delay != last_retry_delay:
                console.print(f"[dim]Retrying in {delay:.0f}s (Ctrl+C to stop)...[/dim]")
                last_retry_delay = delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)


async def _not_connected(_: str) -> None:
    """Placeholder sender used while the relay connection is down."""
    raise ConnectionError("not connected to the relay")


def _close_reason(exc: websockets.exceptions.ConnectionClosed) -> str:
    frame = exc.rcvd or exc.sent
    if frame is None:
        return "no close frame"
    if frame.code == 1012:
        return "relay restarting"
    return frame.reason or f"code {frame.code}"


async def _read_loop(ws: websockets.ClientConnection, client: FpLocalClient) -> None:
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(msg, dict):
            await client.handle(msg)


@click.command("fp")
@click.option("--agent", required=True, help="Agent name or id to forward through")
@click.option(
    "--to",
    "target",
    required=True,
    metavar="HOST:PORT",
    help="Target as seen by the agent (e.g. localhost:22, or just 22)",
)
@click.option(
    "--port",
    "bind_port",
    type=int,
    default=None,
    help="Local port to listen on (default: target port + 9000, capped)",
)
@click.option(
    "--bind",
    "bind_host",
    default="127.0.0.1",
    help="Local address to bind (default loopback — think before widening)",
)
@click.option("--api-key", default=None, help="API key (else env/config)")
@click.option("--relay-host", default="hle.world", help="Relay host")
@click.option("--relay-port", default=443, type=int, help="Relay port")
def fp(
    agent: str,
    target: str,
    bind_port: int | None,
    bind_host: str,
    api_key: str | None,
    relay_host: str,
    relay_port: int,
) -> None:
    """Forward a TCP port from a remote agent to this machine.

    \b
    Examples:
      hle fp --agent rpi --to 22 --port 9922      # then: ssh -p 9922 root@localhost
      hle fp --agent nas --to 192.168.1.50:5432   # postgres on the agent's LAN

    The agent must allow the target. Loopback is allowed by default; other hosts
    are opt-in per agent in the dashboard.
    """
    target_host, target_port = parse_target(target)
    if bind_port is None:
        # 22 -> 9022, 5432 -> 14432; keeps the mapping guessable and off
        # privileged ports without colliding with the target itself.
        bind_port = target_port + 9000 if target_port + 9000 <= 65535 else target_port + 1000

    key = api_key or _load_api_key()
    if not key:
        console.print("[red]Error:[/red] No API key. Run [cyan]hle auth login[/cyan] first.")
        raise SystemExit(1)

    try:
        asyncio.run(
            _run(
                api_key=key,
                agent=agent,
                target_host=target_host,
                target_port=target_port,
                bind_host=bind_host,
                bind_port=bind_port,
                relay_host=relay_host,
                relay_port=relay_port,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    except OSError as exc:
        console.print(f"[red]Error:[/red] could not bind {bind_host}:{bind_port} — {exc}")
        raise SystemExit(1) from None
