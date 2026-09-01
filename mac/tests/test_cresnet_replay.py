"""Tests for the Cresnet replay tool's stream parser.

The parser is the load-bearing part: every conclusion the tool draws about
whether the adapter transmits is a count it produced. A miscount there would
look exactly like a hardware result, so these check it against hand-built
streams and, when a capture is present, against real bus bytes.
"""

import json
from pathlib import Path

import pytest
from cresnet_replay import (
    POOL_BATH_OFF,
    POOL_BATH_ON,
    Frame,
    decode_channel_map,
    parse,
    summarise,
)

CAPTURES = Path(__file__).resolve().parent.parent / "captures"

POLL = bytes([0x64, 0x00])
REPLY = bytes([0x02, 0x00])


def kinds(stream: bytes) -> list[str]:
    return [f.kind for f in parse(stream)]


def test_poll_reply_pair_parses_cleanly() -> None:
    assert kinds(POLL + REPLY) == ["poll", "reply"]


def test_full_round_has_no_strays() -> None:
    stream = b"".join(bytes([d, 0x00]) + REPLY for d in (0x62, 0x63, 0x64, 0x6A, 0x6F))
    stats = summarise(stream, 1.0)
    assert stats.strays == 0
    assert stats.poll_total == 5
    assert stats.replies == 5
    assert stats.orphans == 0


def test_reply_after_poll_is_not_orphan() -> None:
    frames = parse(POLL + REPLY)
    assert frames[1].kind == "reply"
    assert frames[1].orphan is False


def test_reply_without_poll_is_orphan() -> None:
    """The signal the transmit selftest is built on: an answer we did not see
    a question for means someone else asked it, and the only candidate is us.

    Both replies here count. Two answers in a row means two questions we never
    saw, so charging only the first would undercount by half.
    """
    frames = parse(REPLY + REPLY)
    assert [f.orphan for f in frames] == [True, True]


def test_orphan_survives_a_stray_byte_before_the_reply() -> None:
    """A corrupted address byte must not launder an orphan into a normal
    reply, or bit errors would inflate the transmit verdict."""
    stats = summarise(POLL + REPLY + b"\xf8" + REPLY, 1.0)
    assert stats.strays == 1
    assert stats.orphans == 1


def test_data_frame_payload_is_extracted() -> None:
    frames = parse(POOL_BATH_ON)
    assert frames == [Frame(0, "data", 0x72, bytes.fromhex("1D0000000 6FF".replace(" ", "")))]


def test_pool_bath_frames_differ_only_in_level() -> None:
    assert POOL_BATH_ON[:-1] == POOL_BATH_OFF[:-1]
    assert (POOL_BATH_ON[-1], POOL_BATH_OFF[-1]) == (0xFF, 0x00)


def test_led_frame_parses_as_data() -> None:
    stream = bytes.fromhex("66030007 80".replace(" ", ""))
    assert parse(stream) == [Frame(0, "data", 0x66, bytes.fromhex("000780"))]


@pytest.mark.parametrize("head", [0x02, 0x03, 0x82, 0x83])
@pytest.mark.parametrize("tail", [0x00, 0x80])
def test_reply_variants_count_as_replies(head: int, tail: int) -> None:
    """0x66 in the 09:14 capture and 0x6A in the 11:03 one answer with these.
    Scoring them as strays would put the noise floor an order of magnitude
    higher and bury the transmit signal."""
    stats = summarise(POLL + bytes([head, tail]), 1.0)
    assert stats.replies == 1
    assert stats.strays == 0


def test_parser_resynchronises_after_garbage() -> None:
    stream = POLL + REPLY + b"\xff\xf0" + POLL + REPLY
    stats = summarise(stream, 1.0)
    assert stats.poll_total == 2
    assert stats.replies == 2
    assert stats.strays == 2


def test_truncated_data_frame_does_not_read_past_the_end() -> None:
    """A frame cut off by the end of a window must degrade to strays rather
    than raise or invent a payload."""
    stats = summarise(POOL_BATH_ON[:4], 1.0)
    assert stats.data_frames == []


def _load(name: str) -> bytes:
    path = CAPTURES / name
    if not path.exists():
        pytest.skip(f"{path} not present (captures/ is gitignored)")
    stream = bytearray()
    for line in path.read_text().splitlines():
        stream += bytes.fromhex(json.loads(line)["hex"].replace(" ", ""))
    return bytes(stream)


def test_real_capture_parses_almost_completely() -> None:
    """Regression against 13 minutes of real bus traffic. The framing model
    only earns trust if nearly every byte is accounted for."""
    stats = summarise(_load("20260831T091414-raw.jsonl"), 814.0)
    assert stats.total_bytes > 800_000
    assert stats.stray_pct < 0.05


def test_real_capture_orphan_rate_is_near_zero() -> None:
    """The transmit test's noise floor. If the master ever produced orphans on
    its own, the selftest verdict would be meaningless."""
    stats = summarise(_load("20260831T091414-raw.jsonl"), 814.0)
    assert stats.orphans / stats.replies < 0.001


def test_labelled_capture_frames_are_the_ones_we_replay() -> None:
    """The bytes this tool sends must be exactly what the touchpanel event
    put on the wire, not a rebuild of it."""
    path = CAPTURES / "20260831T110421.jsonl"
    if not path.exists():
        pytest.skip("labelled capture not present")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    raws = {r["note"]: bytes.fromhex(r["frames"][0]["raw"].replace(" ", "")) for r in records}
    assert raws["Turn on"] == POOL_BATH_ON
    assert raws["Turn off"] == POOL_BATH_OFF


def _round(devices: tuple[int, ...] = (0x62, 0x63, 0x64, 0x65, 0x67, 0x6A, 0x6D, 0x6F)) -> bytes:
    return b"".join(bytes([d, 0x00]) + REPLY for d in devices)


def test_healthy_window_reports_no_problems() -> None:
    stats = summarise(_round() * 24, 1.0)
    assert stats.health() == []


def test_corrupt_window_is_rejected() -> None:
    """The failure mode this gate exists for: `02 00` still arrives, so the
    stream looks alive, but the address bytes are gone."""
    stream = (b"\xff\xf8" + REPLY) * 200
    problems = summarise(stream, 1.0).health()
    assert problems
    assert any("unparseable" in p for p in problems)


def test_slow_bus_is_rejected() -> None:
    assert any("rounds/s" in p for p in summarise(_round() * 5, 1.0).health())


def test_unanswered_polls_are_rejected() -> None:
    """Half the devices going quiet means we are missing replies, not that
    the devices left."""
    stream = bytes([0x62, 0x00]) * 200 + _round() * 24
    assert any("answered" in p for p in summarise(stream, 1.0).health())


# `1C` module state assertions. These are the only readback the protocol offers
# and the basis of the transmit detector, so the decoder has to be strict:
# it runs over streams that are up to half corrupt.


def test_channel_map_decodes_a_dimmer() -> None:
    """Module 0x72 as MC2E asserted it: eight channels, Pool Bath (ch6) off."""
    payload = bytes.fromhex("1C0000000101020103000400050006000700")
    assert decode_channel_map(payload) == [
        (0, 0),
        (1, 1),
        (2, 1),
        (3, 0),
        (4, 0),
        (5, 0),
        (6, 0),
        (7, 0),
    ]


def test_channel_map_rejects_other_opcodes() -> None:
    assert decode_channel_map(POOL_BATH_ON[2:]) is None


def test_channel_map_rejects_non_ascending_channels() -> None:
    """The ascending-run requirement is the only thing standing between this
    decoder and garbage, so it has to actually reject."""
    assert decode_channel_map(bytes.fromhex("1C00000005010100")) is None


def test_channel_map_rejects_odd_length_body() -> None:
    assert decode_channel_map(bytes.fromhex("1C00000001")) is None


def test_channel_map_rejects_short_payload() -> None:
    assert decode_channel_map(bytes.fromhex("1C00")) is None


def test_channel_map_does_not_fire_on_random_noise() -> None:
    """The stream this runs over is up to half corrupt; a decoder that
    hallucinated state out of noise would invent light states."""
    import random

    rng = random.Random(20260831)
    noise = bytes(rng.randrange(256) for _ in range(200_000))
    hits = [f for f in parse(noise) if decode_channel_map(f.payload) is not None]
    assert len(hits) < 5
