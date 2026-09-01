"""Tests for cresnetmon.protocol against synthetic byte streams.

Each test hand-traces the state machine described in
CresnetMon/MainForm.cs:141-223 to derive expected events.
"""

from cresnetmon.protocol import MASTER_ADDR, CresnetProtocol, Message, PollTick

DEVICE_A = 0x05
DEVICE_B = 0x07


def feed_all(protocol: CresnetProtocol, data: bytes) -> list[object]:
    """Feed every byte, collect the non-None events produced."""
    events: list[object] = []
    for b in data:
        event = protocol.feed(b)
        if event is not None:
            events.append(event)
    return events


def test_searching_ignores_noise_until_zero_sync_byte() -> None:
    protocol = CresnetProtocol()
    events = feed_all(protocol, bytes([0x99, 0x01, 0x00]))
    assert events == []


def test_poll_tick_on_first_reference_device() -> None:
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00]))  # sync to READY

    events = feed_all(protocol, bytes([DEVICE_A, 0x00]))  # empty (poll) frame

    assert events == [PollTick(cycle=1)]


def test_repeated_polls_from_reference_device_keep_ticking() -> None:
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))  # first tick -> cycle 1

    events = feed_all(protocol, bytes([DEVICE_A, 0x00]))

    assert events == [PollTick(cycle=2)]


def test_poll_from_non_reference_device_does_not_tick() -> None:
    protocol = CresnetProtocol()
    # DEVICE_A becomes the poll reference on the first empty frame.
    feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))

    events = feed_all(protocol, bytes([DEVICE_B, 0x00]))

    assert events == []


def test_poll_addressed_to_master_does_not_tick() -> None:
    protocol = CresnetProtocol()
    events = feed_all(protocol, bytes([0x00, MASTER_ADDR, 0x00]))
    assert events == []


def test_message_from_device_to_master() -> None:
    protocol = CresnetProtocol()
    # Prime send_id via a poll frame from DEVICE_A (also produces a tick).
    feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))

    events = feed_all(protocol, bytes([MASTER_ADDR, 0x03, 0x11, 0x22, 0x33]))

    assert events == [
        Message(
            cycle=1,
            text="11 22 33",
            dev_id=DEVICE_A,
            to_master=True,
            dest_id=MASTER_ADDR,
            raw=bytes([MASTER_ADDR, 0x03, 0x11, 0x22, 0x33]),
        ),
    ]


def test_message_from_master_to_device() -> None:
    protocol = CresnetProtocol()

    events = feed_all(protocol, bytes([0x00, DEVICE_A, 0x02, 0xAA, 0xBB]))

    assert events == [
        Message(
            cycle=0,
            text="AA BB",
            dev_id=DEVICE_A,
            to_master=False,
            dest_id=DEVICE_A,
            raw=bytes([DEVICE_A, 0x02, 0xAA, 0xBB]),
        ),
    ]


def test_message_dest_id_is_inferred_only_when_to_master() -> None:
    """STRATEGY.md task 13: dev_id on a to-master message is send_id, an
    inference from the last non-master address seen - not a byte this
    frame itself carried. dest_id is always the byte actually read, so a
    later reader can tell inferred source (to_master, dest_id==MASTER_ADDR)
    from read destination (not to_master, dest_id==dev_id) without having
    to trust dev_id's provenance blindly."""
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))  # primes send_id = DEVICE_A

    to_master = feed_all(protocol, bytes([MASTER_ADDR, 0x01, 0xAA]))[0]
    assert isinstance(to_master, Message)
    assert to_master.to_master is True
    assert to_master.dest_id == MASTER_ADDR  # read directly
    assert to_master.dev_id == DEVICE_A  # inferred - not itself read this frame

    to_device = feed_all(protocol, bytes([DEVICE_B, 0x01, 0xBB]))[0]
    assert isinstance(to_device, Message)
    assert to_device.to_master is False
    assert to_device.dest_id == DEVICE_B  # read directly
    assert to_device.dev_id == DEVICE_B  # same value - not an inference here


def test_message_read_at_defaults_to_none() -> None:
    """protocol.py never reads a clock (module docstring's purity rule) -
    attaching a real wall-clock value is serial_io.SerialReader's job."""
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00]))

    events = feed_all(protocol, bytes([DEVICE_A, 0x01, 0xAA]))

    assert isinstance(events[0], Message)
    assert events[0].read_at is None


def test_oversized_length_byte_resyncs_to_searching() -> None:
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00, DEVICE_A]))  # -> ADDRESSED

    events = feed_all(protocol, bytes([0xFF]))  # > MAX_MSG_SIZE
    assert events == []

    # Now back in SEARCHING: needs a fresh 0x00 sync before addressing works.
    events = feed_all(protocol, bytes([DEVICE_A, 0x00]))
    assert events == []
    events = feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))
    assert events == [PollTick(cycle=1)]


def test_clear_counts_resets_counter_and_optionally_poll_reference() -> None:
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))  # cycle -> 1, ref -> DEVICE_A

    protocol.clear_counts(keep_poll_reference=True)
    assert protocol.msg_count == 0

    # Reference kept: DEVICE_A still ticks, counter restarts at 1.
    events = feed_all(protocol, bytes([DEVICE_A, 0x00]))
    assert events == [PollTick(cycle=1)]

    protocol.clear_counts(keep_poll_reference=False)
    # Reference dropped: DEVICE_B can become the new reference.
    events = feed_all(protocol, bytes([DEVICE_B, 0x00]))
    assert events == [PollTick(cycle=1)]


def test_start_resets_in_flight_parse_without_touching_counts() -> None:
    protocol = CresnetProtocol()
    feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))  # cycle -> 1
    feed_all(protocol, bytes([MASTER_ADDR, 0x05]))  # mid-payload, expects 5 bytes

    protocol.start()

    assert protocol.msg_count == 1  # counts untouched by start()
    # Parser needs a fresh sync; a stray non-zero byte should not address.
    events = feed_all(protocol, bytes([DEVICE_A]))
    assert events == []
    events = feed_all(protocol, bytes([0x00, DEVICE_A, 0x00]))
    assert events == [PollTick(cycle=2)]
