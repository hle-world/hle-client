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
import ipaddress
import json
import logging
import os
from dataclasses import dataclass

import click
import websockets

from hle_client import __version__
from hle_client.firepuncher import FpLocalClient
from hle_client.richcompat import Console
from hle_client.tunnel import _load_api_key
from hle_common.fp_protocol import (
    LOCAL_NETWORKS,
    FpHello,
    FpWelcome,
    is_allowed,
    parse_rule,
)

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


def _explain_if_not_allowed(allowed: list[str], host: str, port: int) -> str | None:
    """A refusal to print before offering the forward, or None if it will work.

    The agent sends its allowlist in the welcome, and the old code printed it
    and then ignored it — announcing the forward and suggesting an ssh command
    for a target it already knew would be refused. The failure then surfaced as
    "Connection closed", minutes later, pointing at nothing.

    An unreadable allowlist means staying quiet: the agent is the authority, so
    guessing "refused" here would block a forward that would have worked.
    """
    if not allowed:
        return None
    if any(entry.startswith(LOCAL_NETWORKS) for entry in allowed):
        # Only the agent can expand "the networks I am on". Reading it here
        # would describe this laptop's networks, which is the wrong machine.
        return None
    try:
        ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        # A name, which the agent resolves when it dials. Judging it against
        # CIDR rules from here would decide on this machine's answer and then
        # let the agent use its own.
        return None
    rules = [rule for rule in (parse_rule(entry) for entry in allowed) if rule is not None]
    if len(rules) != len(allowed):
        return None
    if is_allowed(rules, host, port):
        return None
    return (
        f"[red]Refused:[/red] the agent does not allow {host}:{port}.\n"
        f"[dim]It allows: {', '.join(allowed)}[/dim]\n"
        "[dim]Add a rule for this target on the agent's Firepuncher settings "
        "in the dashboard.[/dim]"
    )


@dataclass(frozen=True)
class Forward:
    """One local port mapped to one target as the agent sees it."""

    target_host: str
    target_port: int
    bind_host: str
    bind_port: int

    def __str__(self) -> str:
        return f"{self.bind_host}:{self.bind_port} → {self.target_host}:{self.target_port}"


def derive_bind_port(target_port: int) -> int:
    """A guessable local port for a target, off the privileged range."""
    # 22 -> 9022, 5432 -> 14432; keeps the mapping readable without colliding
    # with the target itself.
    return target_port + 9000 if target_port + 9000 <= 65535 else target_port + 1000


def _build_forwards(
    targets: tuple[str, ...], bind_ports: tuple[int, ...], bind_host: str
) -> list[Forward]:
    """Pair each ``--to`` with a ``--port``, deriving the ones not given.

    Positional pairing keeps the syntax flat: two repeated flags rather than a
    nested "host:port:localport" form nobody can read back a week later.
    """
    if len(bind_ports) > len(targets):
        raise click.BadParameter(
            f"{len(bind_ports)} --port values for {len(targets)} --to targets; "
            "each --port is paired with the --to in the same position."
        )
    forwards: list[Forward] = []
    for index, target in enumerate(targets):
        host, port = parse_target(target)
        local = bind_ports[index] if index < len(bind_ports) else derive_bind_port(port)
        forwards.append(
            Forward(target_host=host, target_port=port, bind_host=bind_host, bind_port=local)
        )

    seen: dict[int, Forward] = {}
    for forward in forwards:
        clash = seen.get(forward.bind_port)
        if clash is not None:
            raise click.BadParameter(
                f"Local port {forward.bind_port} is wanted by two forwards "
                f"({clash.target_host}:{clash.target_port} and "
                f"{forward.target_host}:{forward.target_port}). Give one an explicit --port."
            )
        seen[forward.bind_port] = forward
    return forwards


def _looks_like_target(value: str) -> bool:
    """Whether *value* could be a forward target rather than a command word.

    Used only to find where the targets stop and a trailing command starts.
    ``parse_target`` is still what validates the ones that are kept, so this
    can be permissive without letting a bad target through.
    """
    try:
        parse_target(value)
    except click.BadParameter:
        return False
    return True


def _split_positionals(
    agent: str | None,
    targets: tuple[str, ...],
    command: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Read `hle forward <agent> <target>...` out of the trailing arguments.

    The command already collects everything after its options so that
    ``-- ssh ...`` can be passed through untouched, which means the positional
    agent and targets arrive in the same bucket. They are whatever precedes
    the ``--`` separator; anything after it is the command to run.

    Explicit ``--agent`` / ``--to`` win, so the old form is unaffected: if both
    were given, every positional is part of the command.
    """
    if agent and targets:
        return agent, targets, command

    leading: list[str] = []
    rest: list[str] = list(command)
    if not agent and rest and rest[0] != "--":
        leading.append(rest.pop(0))
    # `--` cannot be relied on to still be here: with ignore_unknown_options,
    # Click drops it unless the next token looks like an option, so
    # `hle forward rpi 22 -- ssh me@host` arrives with no separator at all.
    # A target has a recognisable shape (a bare port, or host:port); the first
    # argument that does not have it is where the command begins.
    while rest and rest[0] != "--" and _looks_like_target(rest[0]):
        leading.append(rest.pop(0))

    if not leading:
        return agent, targets, tuple(rest)

    if not agent:
        agent = leading.pop(0)
    if leading:
        targets = (*targets, *leading)
    return agent, targets, tuple(rest)


def _resolve_command(command: tuple[str, ...], forwards: list[Forward]) -> list[str] | None:
    """Substitute ``{port}`` / ``{portN}`` in the command to run, if there is one.

    Derived ports are the reason this exists: a command cannot hardcode a port
    the caller never chose, and making people pass --port just to name it in
    their own command defeats deriving it at all.
    """
    if not command:
        return None
    argv = list(command)
    # Click keeps a literal "--" only when it precedes an option-looking token;
    # either way it is a separator, never an argument to run.
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise click.BadParameter("No command given after --")

    replacements = {"{port}": str(forwards[0].bind_port)}
    for index, forward in enumerate(forwards, start=1):
        replacements[f"{{port{index}}}"] = str(forward.bind_port)

    resolved: list[str] = []
    for arg in argv:
        for token, value in replacements.items():
            arg = arg.replace(token, value)
        resolved.append(arg)
    return resolved


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
    forward: Forward,
    relay_host: str,
    relay_port: int,
    label: str = "",
    ready: asyncio.Event | None = None,
) -> None:
    """Serve the local port, reconnecting to the relay as needed.

    The listener is bound once and kept for the lifetime of the command, so a
    relay restart or a dropped network doesn't take the local port away. Streams
    in flight when the connection drops cannot be recovered — the far end is
    gone — so they're closed and the caller reconnects, but the port stays
    there ready for them.
    """
    target_host, target_port = forward.target_host, forward.target_port
    bind_host, bind_port = forward.bind_host, forward.bind_port
    tag = f"[cyan]{label}[/cyan] " if label else ""

    uri = fp_uri(relay_host, relay_port)
    client = FpLocalClient(
        send=_not_connected,
        target_host=target_host,
        target_port=target_port,
        on_error=lambda detail: console.print(f"{tag}[red]Refused:[/red] {detail}"),
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
                        # The agent has just told us what it will accept, so
                        # check now rather than printing "Try: ssh ..." for a
                        # forward already known to be refused and letting the
                        # failure arrive as a closed connection minutes later.
                        refusal = _explain_if_not_allowed(welcome.allowed, target_host, target_port)
                        if refusal is not None:
                            console.print(f"{tag}{refusal}")
                            raise SystemExit(1)

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
                        if ready is not None:
                            ready.set()
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


async def _run_all(
    *,
    api_key: str,
    agent: str,
    forwards: list[Forward],
    relay_host: str,
    relay_port: int,
    command: list[str] | None,
) -> int:
    """Run every forward, and optionally a command whose exit ends them all.

    One relay connection per forward rather than one shared session. Streams
    are already multiplexed per connection, but a shared one would need every
    frame routed back to the forward that owns its stream, and a single drop
    would take all of them down together. Independent connections reconnect
    independently, which is what someone with two forwards actually wants.
    """
    ready = [asyncio.Event() for _ in forwards]
    tasks = [
        asyncio.create_task(
            _run(
                api_key=api_key,
                agent=agent,
                forward=forward,
                relay_host=relay_host,
                relay_port=relay_port,
                label=f"{forward.target_host}:{forward.target_port}" if len(forwards) > 1 else "",
                ready=event,
            )
        )
        for forward, event in zip(forwards, ready, strict=True)
    ]

    if command is None:
        # Interactive: run until interrupted, or until every forward has given
        # up for a reason that will not improve.
        try:
            await asyncio.gather(*tasks)
        finally:
            await _cancel_all(tasks)
        return 0

    try:
        # Don't launch the command against a port nothing is listening on yet.
        # A forward that never comes up would otherwise hang here forever, so
        # a failed task ends the wait as surely as a ready one.
        waiters = [asyncio.create_task(event.wait()) for event in ready]
        done, pending = await asyncio.wait([*waiters, *tasks], return_when=asyncio.FIRST_COMPLETED)
        while not all(event.is_set() for event in ready):
            if any(task.done() for task in tasks):
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()  # type: ignore[misc]
                console.print("[red]Error:[/red] a forward stopped before it was ready.")
                return 1
            done, pending = await asyncio.wait(
                [*waiters, *tasks], return_when=asyncio.FIRST_COMPLETED
            )
        for waiter in waiters:
            waiter.cancel()

        console.print(f"[dim]Running:[/dim] {' '.join(command)}")
        return await _run_command(command, forwards)
    finally:
        await _cancel_all(tasks)


async def _run_command(command: list[str], forwards: list[Forward]) -> int:
    """Run *command* to completion with the forward ports in its environment."""
    env = dict(os.environ)
    env["HLE_FP_PORT"] = str(forwards[0].bind_port)
    env["HLE_FP_HOST"] = forwards[0].bind_host
    for index, forward in enumerate(forwards, start=1):
        env[f"HLE_FP_PORT_{index}"] = str(forward.bind_port)

    try:
        proc = await asyncio.create_subprocess_exec(*command, env=env)
    except FileNotFoundError:
        console.print(f"[red]Error:[/red] command not found: {command[0]}")
        return 127
    except OSError as exc:
        console.print(f"[red]Error:[/red] could not run {command[0]}: {exc}")
        return 126

    try:
        return await proc.wait()
    except asyncio.CancelledError:
        # Ctrl+C reaches the child too, since it shares this terminal's process
        # group. Wait for it rather than tearing the forwards out from under a
        # command that is still shutting down.
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        raise


async def _cancel_all(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


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


@click.command(
    "fp",
    context_settings={"ignore_unknown_options": True},
)
@click.option("--agent", default=None, help="Agent to forward through (or the first argument)")
@click.option(
    "--to",
    "targets",
    multiple=True,
    metavar="HOST:PORT",
    help="Target as seen by the agent (e.g. localhost:22, or just 22). Repeatable.",
)
@click.option(
    "--port",
    "bind_ports",
    type=int,
    multiple=True,
    help="Local port to listen on. Repeatable; paired with --to in order. "
    "Any not given are derived from the target port.",
)
@click.option(
    "--bind",
    "bind_host",
    default="127.0.0.1",
    help="Local address to bind (default loopback — think before widening)",
)
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key (also reads HLE_API_KEY env var, then ~/.config/hle/config.toml)",
)
@click.option("--relay-host", default="hle.world", help="Relay host")
@click.option("--relay-port", default=443, type=int, help="Relay port")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def fp(
    agent: str | None,
    targets: tuple[str, ...],
    bind_ports: tuple[int, ...],
    bind_host: str,
    api_key: str | None,
    relay_host: str,
    relay_port: int,
    command: tuple[str, ...],
) -> None:
    """Forward TCP ports from a remote agent to this machine.

    AGENT is the agent to reach through; each TARGET is a port or HOST:PORT as
    the agent sees it. Both were required flags; a flag you must always pass is
    an argument wearing a costume. `--agent` and `--to` still work.

    \b
    Examples:
      hle forward rpi 22 --port 9922        # then: ssh -p 9922 root@localhost
      hle forward nas 192.168.1.50:5432     # postgres on the agent's LAN
      hle forward rpi 22 nas:445            # two forwards, one command

    \b
    Run a command and tear the forwards down when it exits:
      hle forward rpi 22 -- ssh -p '{port}' me@127.0.0.1
      hle forward rpi 22 5432 -- ./backup.sh

    \b
    In the command, {port} is the first local port and {port1}, {port2}, ...
    are each forward's, so a derived port needn't be guessed. The same values
    arrive as HLE_FP_PORT and HLE_FP_PORT_1, HLE_FP_PORT_2, ... for scripts.
    The exit status is the command's.

    The agent must allow each target. By default that is every network the
    agent is attached to — its LAN, Docker, Kubernetes, any VPN — but not the
    public internet; widen or narrow it per agent in the dashboard.
    """
    agent, targets, command = _split_positionals(agent, targets, command)
    if not agent:
        raise click.UsageError(
            "No agent given. Name it first: hle forward <agent> <target>\n"
            "Run 'hle agent list' to see the agents on your account."
        )
    if not targets:
        raise click.UsageError(
            f"No target given. hle forward {agent} <port|host:port>\n"
            "The target is a port on the agent's side, e.g. 22 or 192.168.1.50:5432."
        )

    forwards = _build_forwards(targets, bind_ports, bind_host)

    key = api_key or _load_api_key()
    if not key:
        console.print("[red]Error:[/red] No API key. Run [cyan]hle auth login[/cyan] first.")
        raise SystemExit(1)

    argv = _resolve_command(command, forwards)

    try:
        exit_code = asyncio.run(
            _run_all(
                api_key=key,
                agent=agent,
                forwards=forwards,
                relay_host=relay_host,
                relay_port=relay_port,
                command=argv,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
        return
    except OSError as exc:
        console.print(f"[red]Error:[/red] could not bind — {exc}")
        raise SystemExit(1) from None
    if exit_code:
        raise SystemExit(exit_code)
