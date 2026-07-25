"""``hle fp`` — firepuncher: forward a remote agent's TCP port to a local one.

    hle fp --agent rpi --to localhost:22 --port 9922
    ssh -p 9922 root@localhost

The connection is private end to end: this client authenticates to the relay
with your API key, the relay pairs it to your agent, and the agent only dials
targets on its allowlist. Nothing listens on a public port.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    uri = fp_uri(relay_host, relay_port)
    async with websockets.connect(uri, max_size=WS_MAX_MESSAGE_SIZE) as ws:
        await ws.send(
            FpHello(api_key=api_key, agent=agent, client_version=__version__).model_dump_json()
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
        first = json.loads(raw)
        if first.get("type") == "fp_error":
            console.print(f"[red]Error:[/red] {first.get('message') or first.get('code')}")
            raise SystemExit(1)
        welcome = FpWelcome.model_validate(first)

        client = FpLocalClient(
            send=ws.send,
            target_host=target_host,
            target_port=target_port,
            on_error=lambda detail: console.print(f"[red]Refused:[/red] {detail}"),
        )
        server = await client.serve(bind_host, bind_port)

        console.print(
            f"[green]Forwarding[/green] {bind_host}:{bind_port} "
            f"→ [cyan]{agent}[/cyan] → {target_host}:{target_port}"
        )
        if welcome.allowed:
            console.print(f"[dim]Agent allows: {', '.join(welcome.allowed)}[/dim]")
        if target_port == 22:
            console.print(f"[dim]Try: ssh -p {bind_port} <user>@{bind_host}[/dim]")
        console.print("[dim]Ctrl+C to stop.[/dim]")

        pump = asyncio.create_task(_read_loop(ws, client))
        try:
            async with server:
                await pump
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await client.close_all()


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
