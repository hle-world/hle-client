"""Envelope fragmentation over real sockets.

The relay here is a plain ``websockets`` server capped at the production 4 MB
per-message limit, doing on its side exactly what the real relay has to do with
``hle_common.fragmentation``. Anything that crosses it whole above 4 MB would
kill the connection, so these tests pass only if fragmentation really happens,
in both directions, for every message type exercised.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from hle_client import agent_update
from hle_client.agent import AgentClient
from hle_client.fragmenting import FragmentingConnection
from hle_client.tunnel import WS_MAX_MESSAGE_SIZE, Tunnel, TunnelConfig
from hle_common.fragmentation import (
    CAPABILITY_FRAGMENTATION,
    FRAGMENT_THRESHOLD,
    MAX_MESSAGE_SIZE,
    Fragment,
    Fragmenter,
    MessageTooLargeError,
    Reassembler,
    encode_fragment,
    is_fragment_frame,
)
from hle_common.models import (
    ProxiedHttpRequest,
    ProxiedHttpResponse,
    TunnelRegistrationResponse,
    WsStreamFrame,
    WsStreamOpen,
)
from hle_common.protocol import MessageType, ProtocolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

API_KEY = "hle_" + "0" * 32


@contextlib.asynccontextmanager
async def _serve(handler: Callable[[Any], Awaitable[None]], **kw: Any):
    server = await websockets.serve(handler, "127.0.0.1", 0, **kw)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- #
# The connection class alone
# --------------------------------------------------------------------------- #
class TestFragmentingConnection:
    async def test_recv_reassembles_and_passes_plain_messages_through(self):
        big = json.dumps({"type": "ping", "pad": "p" * (3 * 1024 * 1024)})

        async def handler(ws):
            await ws.send("before")
            for frame in Fragmenter().frames(big):
                await ws.send(frame)
            await ws.send(b"binary")
            await ws.send("after")
            await ws.close()

        async with (
            _serve(handler) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}",
                max_size=WS_MAX_MESSAGE_SIZE,
                create_connection=FragmentingConnection,
            ) as ws,
        ):
            assert await ws.recv() == "before"
            received = [m async for m in ws]
        assert received == [big, b"binary", "after"]

    async def test_bad_fragment_is_dropped_and_the_channel_stays_up(self):
        async def handler(ws):
            await ws.send(encode_fragment(Fragment(frag_id="x", seq=3, final=False, data="?")))
            await ws.send(encode_fragment(Fragment(frag_id="x", seq=4, final=True, data="?")))
            await ws.send("next")
            await ws.close()

        async with (
            _serve(handler) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}", create_connection=FragmentingConnection
            ) as ws,
        ):
            assert await ws.recv() == "next"

    async def test_partial_buffers_are_freed_on_disconnect(self):
        async def handler(ws):
            await ws.send(encode_fragment(Fragment(frag_id="p", seq=0, final=False, data="x" * 99)))
            await ws.send("next")
            await ws.close()

        async with (
            _serve(handler) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}", create_connection=FragmentingConnection
            ) as ws,
        ):
            assert await ws.recv() == "next"
            assert ws._reassembler.in_flight == 99
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await ws.recv()
        assert ws._reassembler.in_flight == 0
        assert ws._reassembler.partial_count == 0

    async def test_send_is_byte_identical_until_enabled(self):
        got: list[str | bytes] = []
        big = json.dumps({"type": "ping", "pad": "q" * (2 * 1024 * 1024)})

        async def handler(ws):
            got.extend([m async for m in ws])

        async with (
            _serve(handler, max_size=None) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}", create_connection=FragmentingConnection
            ) as ws,
        ):
            assert not ws.fragmentation_enabled
            await ws.send(big)
            await ws.send(b"bytes")
        assert got == [big, b"bytes"]

    async def test_enabled_send_fragments_under_the_transport_cap(self):
        frames: list[str | bytes] = []
        big = json.dumps({"type": "ping", "pad": "r" * (10 * 1024 * 1024)})
        small = json.dumps({"type": "pong"})

        async def handler(ws):
            frames.extend([m async for m in ws])

        async with (
            _serve(handler, max_size=WS_MAX_MESSAGE_SIZE) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}", create_connection=FragmentingConnection
            ) as ws,
        ):
            ws.enable_fragmentation()
            await ws.send(big)
            await ws.send(small)
            await ws.send(b"raw-bytes-untouched")

        assert frames[-2:] == [small, b"raw-bytes-untouched"]
        fragment_frames = frames[:-2]
        assert len(fragment_frames) > 1
        assert all(is_fragment_frame(f) for f in fragment_frames)
        reassembler = Reassembler()
        out = [reassembler.feed_frame(f) for f in fragment_frames]  # type: ignore[arg-type]
        assert out[-1] == big
        assert out[:-1] == [None] * (len(out) - 1)

    async def test_too_large_raises_before_sending_anything(self):
        frames: list[str | bytes] = []

        async def handler(ws):
            frames.extend([m async for m in ws])

        async with (
            _serve(handler) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}", create_connection=FragmentingConnection
            ) as ws,
        ):
            ws.enable_fragmentation()
            with pytest.raises(MessageTooLargeError):
                await ws.send("z" * (MAX_MESSAGE_SIZE + 1))
            await ws.send("still-open")
        assert frames == ["still-open"]

    async def test_cancelled_send_still_delivers_the_whole_message(self, monkeypatch):
        """A stock send() of one frame is atomic under cancellation; a run of
        fragments must be too, or the peer parks a partial it can never finish
        (pinning memory and a partial slot until idle expiry)."""
        from websockets.asyncio.connection import Connection

        frames: list[str | bytes] = []
        big = json.dumps({"type": "ping", "pad": "c" * (8 * 1024 * 1024)})
        gate = asyncio.Event()
        stock_send = Connection.send

        async def slow_send(self, message, *, text=None):
            # Stand-in for backpressure: block once, right after the first slice.
            if isinstance(message, str) and '"seq":1,' in message[:200]:
                await gate.wait()
            await stock_send(self, message, text=text)

        monkeypatch.setattr(Connection, "send", slow_send)

        async def handler(ws):
            frames.extend([m async for m in ws])

        async with (
            _serve(handler, max_size=WS_MAX_MESSAGE_SIZE) as port,
            websockets.connect(
                f"ws://127.0.0.1:{port}", create_connection=FragmentingConnection
            ) as ws,
        ):
            ws.enable_fragmentation()
            task = asyncio.create_task(ws.send(big))
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            gate.set()
            await ws.send("after")
            await asyncio.sleep(0.2)

        reassembler = Reassembler()
        whole = [
            out
            for f in frames
            if isinstance(f, str) and is_fragment_frame(f)
            if (out := reassembler.feed_frame(f)) is not None
        ]
        assert whole == [big]
        assert "after" in frames


# --------------------------------------------------------------------------- #
# Tunnel ↔ fake relay
# --------------------------------------------------------------------------- #
class _Relay:
    """The relay's side of one tunnel connection, using hle_common as-is."""

    def __init__(self, ws: Any, fragmenting: bool) -> None:
        self.ws = ws
        self.fragmenter = Fragmenter() if fragmenting else None
        self.reassembler = Reassembler()
        self.frames_in = 0
        self.fragments_in = 0
        self.raw_in: list[str] = []

    async def send(self, msg: ProtocolMessage) -> None:
        text = msg.model_dump_json()
        frames = self.fragmenter.frames(text) if self.fragmenter else iter((text,))
        for frame in frames:
            await self.ws.send(frame)

    async def recv(self, *types: MessageType) -> ProtocolMessage:
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=20)
            self.frames_in += 1
            if is_fragment_frame(raw):
                self.fragments_in += 1
                whole = self.reassembler.feed_frame(raw)
                if whole is None:
                    continue
                raw = whole
            else:
                self.raw_in.append(raw)
            msg = ProtocolMessage.model_validate_json(raw)
            if not types or msg.type in types:
                return msg


async def _run_tunnel(
    service_url: str,
    server_caps: list[str],
    scenario: Callable[[_Relay, Tunnel], Awaitable[None]],
) -> dict[str, Any]:
    record: dict[str, Any] = {}
    tunnel = Tunnel(TunnelConfig(service_url=service_url, service_label="app", api_key=API_KEY))

    async def handler(ws):
        try:
            reg = ProtocolMessage.model_validate_json(await ws.recv())
            record["capabilities"] = (reg.payload or {}).get("capabilities")
            ack = TunnelRegistrationResponse(
                tunnel_id="t1",
                subdomain="app-x",
                public_url="https://app-x.hle.world",
                websocket_enabled=True,
                user_code="x",
                service_label="app",
                server_capabilities=server_caps,
            )
            await ws.send(
                ProtocolMessage(
                    type=MessageType.TUNNEL_ACK, payload=ack.model_dump()
                ).model_dump_json()
            )
            relay = _Relay(ws, CAPABILITY_FRAGMENTATION in server_caps)
            record["relay"] = relay
            await scenario(relay, tunnel)
        except BaseException as exc:  # surfaced to the test below
            record["error"] = exc
        finally:
            await ws.close()

    # The production per-message cap: nothing over 4 MB may cross whole.
    async with _serve(handler, max_size=WS_MAX_MESSAGE_SIZE) as port:
        with patch.object(
            Tunnel, "_discover_relay_uri", AsyncMock(return_value=f"ws://127.0.0.1:{port}")
        ):
            await tunnel._proxy.start()
            try:
                await asyncio.wait_for(tunnel._connect_once(), timeout=60)
            finally:
                await tunnel._cleanup()
                await tunnel._proxy.stop()
    if "error" in record:
        raise record["error"]
    return record


@contextlib.asynccontextmanager
async def _echo_http():
    """A local HTTP service that echoes the request body (or serves N bytes)."""

    async def handler(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        path = lines[0].split(" ")[1]
        length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        body = await reader.readexactly(length) if length else b""
        if path.startswith("/bytes/"):
            body = b"b" * int(path.rsplit("/", 1)[1])
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def _echo_ws(record: dict[str, Any]):
    """A local WS service that echoes every message back."""

    async def handler(ws):
        try:
            async for message in ws:
                record.setdefault("sizes", []).append(len(message))
                await ws.send(message)
        except websockets.exceptions.ConnectionClosed as exc:
            record["close_code"] = exc.rcvd.code if exc.rcvd else None
        else:
            record["close_code"] = ws.close_code

    async with _serve(handler, max_size=None) as port:
        yield f"http://127.0.0.1:{port}"


def _http_request(request_id: str, method: str, path: str, body: bytes = b"") -> ProtocolMessage:
    req = ProxiedHttpRequest(
        request_id=request_id,
        method=method,
        path=path,
        headers={"content-type": "application/octet-stream"},
        body=base64.b64encode(body).decode("ascii") if body else None,
    )
    return ProtocolMessage(
        type=MessageType.HTTP_REQUEST,
        tunnel_id="t1",
        request_id=request_id,
        payload=req.model_dump(),
    )


class TestTunnelFragmentation:
    async def test_registration_advertises_fragmentation(self):
        async def scenario(relay: _Relay, tunnel: Tunnel) -> None:
            return None

        async with _echo_http() as url:
            record = await _run_tunnel(url, [], scenario)
        assert CAPABILITY_FRAGMENTATION in record["capabilities"]
        assert "chunked_response" in record["capabilities"]

    async def test_large_http_request_and_response(self):
        """6 MB body each way: an 8 MB envelope relay→client and client→relay."""
        body = os.urandom(6 * 1024 * 1024)

        async def scenario(relay: _Relay, tunnel: Tunnel) -> None:
            await relay.send(_http_request("r1", "POST", "/echo", body))
            msg = await relay.recv(MessageType.HTTP_RESPONSE)
            resp = ProxiedHttpResponse.model_validate(msg.payload)
            assert resp.status_code == 200
            assert base64.b64decode(resp.body or "") == body
            assert relay.fragments_in > 1

        async with _echo_http() as url:
            await _run_tunnel(url, [CAPABILITY_FRAGMENTATION], scenario)

    async def test_large_ws_frames_both_directions(self):
        binary = os.urandom(6 * 1024 * 1024)
        text = "é" * (5 * 1024 * 1024)
        upstream: dict[str, Any] = {}

        async def scenario(relay: _Relay, tunnel: Tunnel) -> None:
            open_ = WsStreamOpen(stream_id="s1", path="/ws", headers={})
            await relay.send(
                ProtocolMessage(
                    type=MessageType.WS_OPEN, tunnel_id="t1", payload=open_.model_dump()
                )
            )
            await relay.recv(MessageType.WS_ACCEPT)
            for data, is_binary in (
                (base64.b64encode(binary).decode("ascii"), True),
                (text, False),
            ):
                frame = WsStreamFrame(stream_id="s1", data=data, is_binary=is_binary)
                await relay.send(
                    ProtocolMessage(
                        type=MessageType.WS_FRAME, tunnel_id="t1", payload=frame.model_dump()
                    )
                )
            echoed = [
                WsStreamFrame.model_validate((await relay.recv(MessageType.WS_FRAME)).payload)
                for _ in range(2)
            ]
            assert echoed[0].is_binary and base64.b64decode(echoed[0].data) == binary
            assert not echoed[1].is_binary and echoed[1].data == text
            assert relay.fragments_in > 2

        async with _echo_ws(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_FRAGMENTATION], scenario)
        # The upstream really exchanged messages above the old 4 MB cap.
        assert upstream["sizes"] == [len(binary), len(text)]

    async def test_upstream_message_too_large_to_fragment_closes_only_its_stream(self):
        upstream: dict[str, Any] = {}

        async def scenario(relay: _Relay, tunnel: Tunnel) -> None:
            conn = tunnel._ws
            assert isinstance(conn, FragmentingConnection)
            for _ in range(100):  # the client enables it once it has read the ACK
                if conn.fragmentation_enabled:
                    break
                await asyncio.sleep(0.01)
            assert conn.fragmentation_enabled
            # Shrink the ceiling so the test need not move 64 MB.
            conn._fragmenter = Fragmenter(max_message_size=FRAGMENT_THRESHOLD * 2)
            open_ = WsStreamOpen(stream_id="s1", path="/ws", headers={})
            await relay.send(
                ProtocolMessage(
                    type=MessageType.WS_OPEN, tunnel_id="t1", payload=open_.model_dump()
                )
            )
            await relay.recv(MessageType.WS_ACCEPT)
            frame = WsStreamFrame(stream_id="s1", data="x" * (3 * 1024 * 1024))
            await relay.send(
                ProtocolMessage(
                    type=MessageType.WS_FRAME, tunnel_id="t1", payload=frame.model_dump()
                )
            )
            close = await relay.recv(MessageType.WS_CLOSE)
            assert close.payload is not None
            assert close.payload["stream_id"] == "s1"
            assert close.payload["code"] == 1009
            # The tunnel itself is still serving (the WS-only upstream answers
            # plain HTTP with 426, which is all this needs).
            await relay.send(_http_request("r2", "GET", "/"))
            resp = await relay.recv(MessageType.HTTP_RESPONSE)
            assert ProxiedHttpResponse.model_validate(resp.payload).request_id == "r2"

        async with _echo_ws(upstream) as ws_url:
            await _run_tunnel(ws_url, [CAPABILITY_FRAGMENTATION], scenario)
        assert upstream["close_code"] == 1009

    async def test_without_the_capability_nothing_changes_on_the_wire(self):
        """An old relay gets one whole envelope, in today's exact bytes."""
        size = 2 * 1024 * 1024  # ~2.7 MB envelope: over the threshold, under the cap

        async def scenario(relay: _Relay, tunnel: Tunnel) -> None:
            await relay.send(_http_request("r1", "GET", f"/bytes/{size}"))
            msg = await relay.recv(MessageType.HTTP_RESPONSE)
            conn = tunnel._ws
            assert isinstance(conn, FragmentingConnection)
            assert not conn.fragmentation_enabled
            assert relay.fragments_in == 0
            raw = relay.raw_in[-1]
            assert len(raw) > FRAGMENT_THRESHOLD
            assert raw == msg.model_dump_json()
            resp = ProxiedHttpResponse.model_validate(msg.payload)
            assert base64.b64decode(resp.body or "") == b"b" * size

        async with _echo_http() as url:
            await _run_tunnel(url, [], scenario)

    async def test_upstream_max_size_follows_negotiation(self):
        async def max_size_for(server_caps: list[str]) -> int:
            tunnel = Tunnel(TunnelConfig(service_url="http://localhost:7681"))
            tunnel._tunnel_id = "t1"
            tunnel._server_caps = server_caps
            local = AsyncMock()
            local.subprotocol = None
            captured: dict[str, Any] = {}

            async def fake_connect(url, **kwargs):
                captured.update(kwargs)
                return local

            open_ = WsStreamOpen(stream_id="s1", path="/ws", headers={})
            msg = ProtocolMessage(type=MessageType.WS_OPEN, payload=open_.model_dump())
            with patch("hle_client.tunnel.websockets.connect", side_effect=fake_connect):
                await tunnel._handle_ws_open(AsyncMock(), msg)
            await tunnel._cleanup()
            return int(captured["max_size"])

        assert await max_size_for([CAPABILITY_FRAGMENTATION]) == MAX_MESSAGE_SIZE
        assert await max_size_for(["chunked_response"]) == WS_MAX_MESSAGE_SIZE


# --------------------------------------------------------------------------- #
# Agent control channel
# --------------------------------------------------------------------------- #
class _FakeTunnel:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.is_connected = False
        self.public_url = None
        self.active_ws_streams = 0

    async def connect(self) -> None:
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        return None


def _welcome(count: int, caps: list[str]) -> str:
    pad = "/" + "p" * 2000
    return json.dumps(
        {
            "type": "welcome",
            "agent_public_id": "pub-1",
            "base_domain": "hle.world",
            "api_key": API_KEY,
            "capabilities": caps,
            "endpoints": [
                {"id": i, "label": f"ep{i}", "service_url": f"http://localhost:{1000 + i}{pad}"}
                for i in range(count)
            ],
        },
        separators=(",", ":"),
    )


async def _run_agent(tmp_path, monkeypatch, welcome: str) -> tuple[AgentClient, dict[str, Any]]:
    import hle_client.agent as agent_mod

    record: dict[str, Any] = {}
    tunnels: list[_FakeTunnel] = []

    def factory(cfg: Any) -> _FakeTunnel:
        t = _FakeTunnel(cfg)
        tunnels.append(t)
        return t

    async def handler(ws):
        try:
            hello = json.loads(await ws.recv())
            record["capabilities"] = hello["capabilities"]
            frames = list(Fragmenter().frames(welcome))
            record["welcome_frames"] = len(frames)
            for frame in frames:
                await ws.send(frame)
            for _ in range(100):
                if client._ws is not None:
                    break
                await asyncio.sleep(0.05)
            record["enabled"] = client._ws.fragmentation_enabled
        except BaseException as exc:
            record["error"] = exc
        finally:
            await ws.close()

    monkeypatch.setattr(agent_mod, "active_providers", list)
    async with _serve(handler, max_size=WS_MAX_MESSAGE_SIZE) as port:
        client = AgentClient(
            "hlea_test",
            relay_host="localhost",
            relay_port=port,
            tunnel_factory=factory,
            home=tmp_path,
            support_probe=lambda: agent_update.UpdateSupport(False, "brew", "unsupported:brew"),
        )

        async def no_discovery(_ws: Any) -> None:
            return None

        monkeypatch.setattr(client, "_report_discovery", no_discovery)
        client._running = True
        try:
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(client._connect_once(), timeout=30)
        finally:
            await client.stop()
    if "error" in record:
        raise record["error"]
    record["tunnels"] = tunnels
    return client, record


class TestAgentFragmentation:
    async def test_large_welcome_is_reassembled_and_sending_enabled(self, tmp_path, monkeypatch):
        welcome = _welcome(700, [CAPABILITY_FRAGMENTATION])
        assert len(welcome) > FRAGMENT_THRESHOLD
        _, record = await _run_agent(tmp_path, monkeypatch, welcome)
        assert CAPABILITY_FRAGMENTATION in record["capabilities"]
        assert record["welcome_frames"] > 1
        assert len(record["tunnels"]) == 700
        assert record["enabled"] is True

    async def test_server_without_the_capability_gets_no_fragments(self, tmp_path, monkeypatch):
        _, record = await _run_agent(tmp_path, monkeypatch, _welcome(2, []))
        assert record["welcome_frames"] == 1
        assert len(record["tunnels"]) == 2
        assert record["enabled"] is False

    async def test_1_4_welcome_without_the_field_is_understood(self, tmp_path, monkeypatch):
        # An old relay's welcome has no `capabilities` key at all.
        old = json.loads(_welcome(2, []))
        del old["capabilities"]
        _, record = await _run_agent(tmp_path, monkeypatch, json.dumps(old))
        assert len(record["tunnels"]) == 2
        assert record["enabled"] is False
