"""Envelope fragmentation: the hle_common wire model, sender and reassembler.

These pin the contract the relay reuses verbatim, so every error path,
boundary and ceiling is exercised here rather than only through the tunnel.
"""

from __future__ import annotations

import json

import pytest

from hle_common.fragmentation import (
    CAPABILITY_FRAGMENTATION,
    FRAGMENT_SIZE,
    FRAGMENT_THRESHOLD,
    MAX_FRAGMENTS_PER_MESSAGE,
    MAX_IN_FLIGHT,
    MAX_MESSAGE_SIZE,
    MAX_PARTIALS,
    Fragment,
    Fragmenter,
    FragmentError,
    FragmentLimitError,
    FragmentProtocolError,
    MessageTooLargeError,
    Reassembler,
    encode_fragment,
    is_fragment_frame,
)
from hle_common.models import ProxiedHttpRequest, WsStreamFrame
from hle_common.protocol import MessageType, ProtocolMessage


def frag(frag_id: str = "1", seq: int = 0, final: bool = False, data: str = "x") -> Fragment:
    return Fragment(frag_id=frag_id, seq=seq, final=final, data=data)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _envelope(size: int) -> str:
    """A real ProtocolMessage envelope of at least *size* characters."""
    data = "a" * size
    return ProtocolMessage(
        type=MessageType.WS_FRAME,
        tunnel_id="t1",
        payload=WsStreamFrame(stream_id="s1", data=data).model_dump(),
    ).model_dump_json()


# --------------------------------------------------------------------------- #
# Constants and wire model
# --------------------------------------------------------------------------- #
class TestContract:
    def test_capability_token(self):
        assert CAPABILITY_FRAGMENTATION == "fragmentation"

    def test_constants(self):
        assert FRAGMENT_SIZE == 256 * 1024
        assert FRAGMENT_THRESHOLD == 1024 * 1024
        assert MAX_MESSAGE_SIZE == 64 * 1024 * 1024
        assert MAX_IN_FLIGHT == 128 * 1024 * 1024
        assert FRAGMENT_SIZE < FRAGMENT_THRESHOLD < MAX_MESSAGE_SIZE < MAX_IN_FLIGHT

    def test_message_type(self):
        assert MessageType.FRAGMENT == "fragment"

    def test_worst_case_fragment_frame_fits_the_transport_cap(self):
        # Every character JSON-escaped to \u00XX is the worst case for one slice.
        frame = encode_fragment(frag(data="\x01" * FRAGMENT_SIZE))
        assert len(frame) < 4 * 1024 * 1024

    def test_frame_shape(self):
        frame = encode_fragment(frag(frag_id="9", seq=2, final=True, data='{"a"'))
        assert json.loads(frame) == {
            "type": "fragment",
            "tunnel_id": None,
            "request_id": None,
            "payload": {"frag_id": "9", "seq": 2, "final": True, "data": '{"a"'},
        }

    def test_is_fragment_frame(self):
        assert is_fragment_frame(encode_fragment(frag()))
        assert not is_fragment_frame(ProtocolMessage(type=MessageType.PING).model_dump_json())
        assert not is_fragment_frame(encode_fragment(frag()).encode())
        assert not is_fragment_frame("")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"frag_id": ""},
            {"frag_id": 5},
            {"seq": -1},
            {"seq": True},
            {"seq": "0"},
            {"final": "yes"},
            {"data": b"x"},
            {"data": None},
        ],
    )
    def test_model_rejects_bad_fields(self, kwargs):
        base = {"frag_id": "1", "seq": 0, "final": False, "data": "x"}
        base.update(kwargs)
        with pytest.raises(ValueError):
            Fragment.model_validate(base)

    def test_model_round_trip(self):
        original = frag(frag_id="a", seq=3, final=True, data='é"\\')
        assert Fragment.model_validate_json(original.model_dump_json()) == original


# --------------------------------------------------------------------------- #
# Sender
# --------------------------------------------------------------------------- #
class TestFragmenter:
    def test_small_envelope_passes_through_unchanged(self):
        env = _envelope(100)
        assert list(Fragmenter().frames(env)) == [env]

    def test_exactly_threshold_is_not_fragmented(self):
        env = "x" * FRAGMENT_THRESHOLD
        out = list(Fragmenter().frames(env))
        assert out == [env]
        assert out[0] is env

    def test_one_over_threshold_is_fragmented(self):
        env = "x" * (FRAGMENT_THRESHOLD + 1)
        frames = list(Fragmenter().frames(env))
        assert all(is_fragment_frame(f) for f in frames)
        assert len(frames) == 5  # 4 full slices + 1 char

    def test_slices_concatenate_back_and_are_ordered(self):
        env = _envelope(3 * 1024 * 1024)
        frames = [ProtocolMessage.model_validate_json(f) for f in Fragmenter().frames(env)]
        payloads = [Fragment.model_validate(m.payload) for m in frames]
        assert [p.seq for p in payloads] == list(range(len(payloads)))
        assert [p.final for p in payloads] == [False] * (len(payloads) - 1) + [True]
        assert len({p.frag_id for p in payloads}) == 1
        assert all(len(p.data) <= FRAGMENT_SIZE for p in payloads)
        assert "".join(p.data for p in payloads) == env

    def test_exact_multiple_of_fragment_size_has_no_empty_tail(self):
        env = "y" * (FRAGMENT_SIZE * 8)
        payloads = [
            Fragment.model_validate(ProtocolMessage.model_validate_json(f).payload)
            for f in Fragmenter(threshold=10).frames(env)
        ]
        assert len(payloads) == 8
        assert payloads[-1].final and len(payloads[-1].data) == FRAGMENT_SIZE

    def test_frag_ids_are_unique_per_message(self):
        f = Fragmenter(threshold=1, fragment_size=2)
        ids = {
            ProtocolMessage.model_validate_json(next(iter(f.frames("abcd")))).payload["frag_id"]
            for _ in range(5)
        }
        assert len(ids) == 5

    def test_over_ceiling_raises_before_yielding(self):
        f = Fragmenter(threshold=4, fragment_size=2, max_message_size=10)
        with pytest.raises(MessageTooLargeError) as info:
            f.frames("z" * 11)
        assert info.value.close_code == 1009
        assert isinstance(info.value, FragmentLimitError)
        assert list(f.frames("z" * 10))  # exactly the ceiling is allowed

    def test_invalid_fragment_size(self):
        with pytest.raises(ValueError):
            Fragmenter(fragment_size=0)

    def test_non_ascii_round_trips(self):
        env = json.dumps({"t": "héllo wörld ✓ " * 50}, ensure_ascii=False)
        r = Reassembler()
        out = None
        for frame in Fragmenter(threshold=10, fragment_size=7).frames(env):
            out = r.feed_frame(frame)
        assert out == env


# --------------------------------------------------------------------------- #
# Receiver
# --------------------------------------------------------------------------- #
def _feed_all(r: Reassembler, frames) -> list[str]:
    out = []
    for frame in frames:
        whole = r.feed_frame(frame)
        if whole is not None:
            out.append(whole)
    return out


class TestReassemblerHappyPath:
    def test_round_trip(self):
        env = _envelope(5 * 1024 * 1024)
        r = Reassembler()
        assert _feed_all(r, Fragmenter().frames(env)) == [env]
        assert r.in_flight == 0
        assert r.partial_count == 0

    def test_single_fragment_message(self):
        r = Reassembler()
        assert r.feed(frag(seq=0, final=True, data="whole")) == "whole"
        assert r.partial_count == 0

    def test_empty_final_slice(self):
        r = Reassembler()
        assert r.feed(frag(seq=0, data="ab")) is None
        assert r.feed(frag(seq=1, final=True, data="")) == "ab"

    def test_interleaved_messages(self):
        r = Reassembler()
        assert r.feed(frag("a", 0, data="A1")) is None
        assert r.feed(frag("b", 0, data="B1")) is None
        assert r.feed(frag("a", 1, final=True, data="A2")) == "A1A2"
        assert r.feed(frag("b", 1, final=True, data="B2")) == "B1B2"
        assert r.in_flight == 0

    def test_in_flight_accounting(self):
        r = Reassembler()
        r.feed(frag("a", 0, data="1234"))
        r.feed(frag("b", 0, data="12"))
        assert r.in_flight == 6
        assert r.partial_count == 2
        r.feed(frag("a", 1, final=True, data="5"))
        assert r.in_flight == 2

    def test_feed_payload(self):
        r = Reassembler()
        assert r.feed_payload({"frag_id": "1", "seq": 0, "final": True, "data": "x"}) == "x"


class TestReassemblerProtocolErrors:
    def test_continuation_without_start(self):
        r = Reassembler()
        with pytest.raises(FragmentProtocolError) as info:
            r.feed(frag(seq=1))
        assert info.value.frag_id == "1"
        assert info.value.close_code == 1002

    def test_gap(self):
        r = Reassembler()
        r.feed(frag(seq=0, data="abc"))
        with pytest.raises(FragmentProtocolError, match="expected seq=1, got 2"):
            r.feed(frag(seq=2))
        assert r.partial_count == 0
        assert r.in_flight == 0

    def test_duplicate(self):
        r = Reassembler()
        r.feed(frag(seq=0))
        r.feed(frag(seq=1))
        with pytest.raises(FragmentProtocolError):
            r.feed(frag(seq=1))
        assert r.in_flight == 0

    def test_duplicate_start(self):
        r = Reassembler()
        r.feed(frag(seq=0))
        with pytest.raises(FragmentProtocolError):
            r.feed(frag(seq=0))
        assert r.partial_count == 0

    def test_out_of_order(self):
        r = Reassembler()
        r.feed(frag(seq=0))
        r.feed(frag(seq=1))
        r.feed(frag(seq=2))
        with pytest.raises(FragmentProtocolError):
            r.feed(frag(seq=1))

    def test_rest_of_abandoned_message_is_swallowed(self):
        r = Reassembler()
        r.feed(frag(seq=0))
        with pytest.raises(FragmentProtocolError):
            r.feed(frag(seq=5))
        assert r.feed(frag(seq=6)) is None
        assert r.feed(frag(seq=7, final=True)) is None
        # Once its final slice has passed, the id is forgotten.
        assert r.feed(frag(seq=0, final=True, data="new")) == "new"

    def test_failure_does_not_disturb_other_messages(self):
        r = Reassembler()
        r.feed(frag("good", 0, data="G"))
        r.feed(frag("bad", 0, data="B"))
        with pytest.raises(FragmentProtocolError):
            r.feed(frag("bad", 3))
        assert r.feed(frag("good", 1, final=True, data="!")) == "G!"

    @pytest.mark.parametrize(
        "payload",
        [None, [], {"frag_id": "1"}, {"frag_id": "1", "seq": "0", "final": True, "data": ""}],
    )
    def test_malformed_payload(self, payload):
        with pytest.raises(FragmentProtocolError):
            Reassembler().feed_payload(payload)

    def test_unparseable_frame(self):
        with pytest.raises(FragmentProtocolError):
            Reassembler().feed_frame('{"type":"fragment",')

    def test_frame_of_another_type(self):
        with pytest.raises(FragmentProtocolError):
            Reassembler().feed_frame(ProtocolMessage(type=MessageType.PING).model_dump_json())

    def test_all_errors_share_a_base(self):
        assert issubclass(FragmentProtocolError, FragmentError)
        assert issubclass(FragmentLimitError, FragmentError)
        assert issubclass(MessageTooLargeError, FragmentError)


class TestReassemblerLimits:
    def test_message_ceiling_exact_is_allowed(self):
        r = Reassembler(max_message_size=10)
        r.feed(frag(seq=0, data="12345"))
        assert r.feed(frag(seq=1, final=True, data="67890")) == "1234567890"

    def test_message_ceiling_checked_before_append(self):
        r = Reassembler(max_message_size=10)
        r.feed(frag(seq=0, data="12345"))
        r.feed(frag(seq=1, data="6789"))
        with pytest.raises(FragmentLimitError) as info:
            r.feed(frag(seq=2, data="ab"))  # would make 11
        assert info.value.close_code == 1009
        assert r.in_flight == 0
        assert r.partial_count == 0
        # Remaining fragments are swallowed, not re-raised.
        assert r.feed(frag(seq=3, final=True, data="z")) is None

    def test_message_ceiling_on_final_slice(self):
        r = Reassembler(max_message_size=4)
        r.feed(frag(seq=0, data="1234"))
        with pytest.raises(FragmentLimitError):
            r.feed(frag(seq=1, final=True, data="5"))
        assert r.in_flight == 0

    def test_single_oversized_slice(self):
        with pytest.raises(FragmentLimitError):
            Reassembler(max_message_size=3).feed(frag(seq=0, final=True, data="1234"))

    def test_in_flight_cap_across_messages(self):
        r = Reassembler(max_message_size=100, max_in_flight=10)
        r.feed(frag("a", 0, data="123456"))
        r.feed(frag("b", 0, data="1234"))  # exactly 10
        with pytest.raises(FragmentLimitError, match="in flight"):
            r.feed(frag("c", 0, data="1"))
        assert r.in_flight == 10  # the others are untouched
        assert r.feed(frag("a", 1, final=True, data="!")) == "123456!"
        assert r.in_flight == 4

    def test_in_flight_cap_drops_the_growing_message(self):
        r = Reassembler(max_message_size=100, max_in_flight=10)
        r.feed(frag("a", 0, data="12345"))
        r.feed(frag("b", 0, data="12345"))
        with pytest.raises(FragmentLimitError):
            r.feed(frag("a", 1, data="6"))
        assert r.in_flight == 5
        assert r.partial_count == 1

    def test_final_slice_completes_even_at_the_in_flight_cap(self):
        # Completing a message frees memory; it must not be refused for it.
        r = Reassembler(max_message_size=100, max_in_flight=5)
        r.feed(frag("a", 0, data="12345"))
        assert r.feed(frag("a", 1, final=True, data="678")) == "12345678"

    def test_max_partials(self):
        r = Reassembler(max_partials=2)
        r.feed(frag("a", 0))
        r.feed(frag("b", 0))
        with pytest.raises(FragmentLimitError, match="partial messages"):
            r.feed(frag("c", 0))
        # A single-slice message needs no partial slot.
        assert r.feed(frag("d", 0, final=True, data="ok")) == "ok"
        assert r.partial_count == 2

    def test_default_max_partials(self):
        r = Reassembler()
        for i in range(MAX_PARTIALS):
            r.feed(frag(str(i), 0))
        with pytest.raises(FragmentLimitError):
            r.feed(frag("one-more", 0))


class TestReassemblerCleanup:
    def test_idle_partials_expire_on_next_feed(self):
        clock = _Clock()
        r = Reassembler(idle_timeout=60.0, clock=clock)
        r.feed(frag("old", 0, data="abc"))
        clock.now += 61
        assert r.feed(frag("new", 0, final=True, data="n")) == "n"
        assert r.partial_count == 0
        assert r.in_flight == 0
        # The expired message's late fragments are swallowed.
        assert r.feed(frag("old", 1, final=True)) is None

    def test_activity_keeps_a_partial_alive(self):
        clock = _Clock()
        r = Reassembler(idle_timeout=60.0, clock=clock)
        r.feed(frag(seq=0, data="a"))
        clock.now += 50
        r.feed(frag(seq=1, data="b"))
        clock.now += 50
        assert r.feed(frag(seq=2, final=True, data="c")) == "abc"

    def test_expire_explicitly(self):
        clock = _Clock()
        r = Reassembler(idle_timeout=1.0, clock=clock)
        r.feed(frag("a", 0))
        r.feed(frag("b", 0))
        assert r.expire() == 0
        clock.now += 2
        assert r.expire() == 2
        assert r.in_flight == 0

    def test_clear_frees_everything(self):
        r = Reassembler()
        r.feed(frag("a", 0, data="x" * 100))
        r.feed(frag("b", 0))
        with pytest.raises(FragmentProtocolError):
            r.feed(frag("c", 4))
        r.clear()
        assert r.in_flight == 0
        assert r.partial_count == 0
        # Nothing is remembered, abandoned ids included.
        assert r.feed(frag("c", 0, final=True, data="c")) == "c"

    def test_abandoned_memory_is_bounded(self):
        r = Reassembler()
        for i in range(5000):
            with pytest.raises(FragmentProtocolError):
                r.feed(frag(str(i), 1))
        assert len(r._abandoned) <= 1024


class TestReassemblerAdversarial:
    """Memory a hostile peer could pin that the character ceilings never count."""

    def test_frag_id_length_is_capped(self):
        # frag_id is a dict key in the partial table and the abandoned set,
        # neither of which counts towards in_flight: 1024 remembered ids of
        # ~4 MB each would otherwise pin ~4 GB.
        Fragment.model_validate({"frag_id": "i" * 64, "seq": 0, "final": True, "data": ""})
        with pytest.raises(ValueError):
            Fragment.model_validate({"frag_id": "i" * 65, "seq": 0, "final": True, "data": ""})
        r = Reassembler()
        raw = json.dumps(
            {
                "type": "fragment",
                "tunnel_id": None,
                "request_id": None,
                "payload": {"frag_id": "i" * 100_000, "seq": 1, "final": False, "data": ""},
            },
            separators=(",", ":"),
        )
        with pytest.raises(FragmentProtocolError):
            r.feed_frame(raw)
        assert not r._abandoned

    def test_slice_count_per_message_is_capped(self):
        # Empty (or one-character) non-final slices cost no in_flight but a
        # list slot plus a str object each; uncapped, one partial grows forever.
        r = Reassembler()
        with pytest.raises(FragmentLimitError):
            for seq in range(MAX_FRAGMENTS_PER_MESSAGE + 1):
                r.feed(frag("z", seq, data=""))
        assert r.partial_count == 0
        # The legitimate worst case — a ceiling-sized message in default
        # slices — stays far inside the cap.
        assert MAX_MESSAGE_SIZE // FRAGMENT_SIZE < MAX_FRAGMENTS_PER_MESSAGE

    @pytest.mark.parametrize("fragment_size", range(1, 14))
    def test_emoji_survives_every_cut_point(self, fragment_size):
        # Envelopes are ensure_ascii JSON, so 😀 is the 12-char "😀"
        # and slices land between (and inside) the two escapes.
        request = ProxiedHttpRequest(
            request_id="r1",
            method="GET",
            path="/😀",
            headers={"x-emoji": "😀 é ✓ 😀"},
            body=None,
        )
        frames = [
            ProtocolMessage(
                type=MessageType.HTTP_REQUEST, request_id="r1", payload=request.model_dump()
            ).model_dump_json(),
            ProtocolMessage(
                type=MessageType.WS_FRAME,
                payload=WsStreamFrame(stream_id="s", data="😀" * 5 + "x").model_dump(),
            ).model_dump_json(),
        ]
        r = Reassembler()
        for env in frames:
            assert env.isascii()
            fragmenter = Fragmenter(threshold=1, fragment_size=fragment_size)
            assert _feed_all(r, fragmenter.frames(env)) == [env]
        whole = ProtocolMessage.model_validate_json(frames[0])
        assert ProxiedHttpRequest.model_validate(whole.payload).headers["x-emoji"] == "😀 é ✓ 😀"
