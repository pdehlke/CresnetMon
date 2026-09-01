"""Tests for cresnetmon.serial_io.SerialReader using a fake port (no real
serial hardware needed)."""

import queue
import time

import pytest

from cresnetmon.protocol import CresnetProtocol, Message, PollTick
from cresnetmon.serial_io import SerialReader


class _FakePort:
    """Minimal stand-in satisfying serial_io.SerialPort: feeds a fixed byte
    sequence, then blocks briefly (like an idle real port with a read
    timeout) until closed."""

    def __init__(self, data: bytes) -> None:
        self.is_open = True
        self._data = bytearray(data)

    def read(self, size: int = 1) -> bytes:
        if not self.is_open or not self._data:
            time.sleep(0.005)
            return b""
        return bytes([self._data.pop(0)])

    def close(self) -> None:
        self.is_open = False


def test_reader_feeds_bytes_and_queues_events() -> None:
    port = _FakePort(bytes([0x00, 0x05, 0x00]))  # sync + empty (poll) frame
    protocol = CresnetProtocol()
    reader = SerialReader(port, protocol)

    reader.start()
    event = reader.events.get(timeout=1)
    reader.stop()

    assert event == PollTick(cycle=1)


def test_reader_ignores_empty_reads_without_erroring() -> None:
    port = _FakePort(bytes([0x00]))  # sync byte only, then idle
    protocol = CresnetProtocol()
    reader = SerialReader(port, protocol)

    reader.start()
    time.sleep(0.05)  # let a few empty reads happen
    reader.stop()

    assert reader.events.empty()


def test_stop_closes_the_port() -> None:
    port = _FakePort(b"")
    protocol = CresnetProtocol()
    reader = SerialReader(port, protocol)

    reader.start()
    reader.stop()
    time.sleep(0.02)

    assert port.is_open is False


def test_start_is_idempotent_while_already_running() -> None:
    port = _FakePort(bytes([0x00, 0x05, 0x00]))
    protocol = CresnetProtocol()
    reader = SerialReader(port, protocol)

    reader.start()
    reader.start()  # should not spawn a second thread / raise
    event = reader.events.get(timeout=1)
    reader.stop()

    assert event == PollTick(cycle=1)


def test_message_events_get_a_wall_clock_read_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """STRATEGY.md task 13: protocol.py never sets read_at (it has no
    clock), so SerialReader._run() must attach it right after feed()
    returns a Message - the earliest point a real timestamp is available."""
    monkeypatch.setattr("cresnetmon.serial_io.time.time", lambda: 1756642872.5)
    port = _FakePort(bytes([0x00, 0x05, 0x01, 0xAA]))  # sync + one-byte message
    protocol = CresnetProtocol()
    reader = SerialReader(port, protocol)

    reader.start()
    event = reader.events.get(timeout=1)
    reader.stop()

    assert isinstance(event, Message)
    assert event.read_at == 1756642872.5


def test_raw_queue_receives_every_byte_read_in_order() -> None:
    """STRATEGY.md task 14: the raw queue is a straight tap on every byte
    read, independent of what protocol.py does with it - including the
    poll-frame bytes protocol.py mostly discards."""
    data = bytes([0x00, 0x05, 0x00, 0x02, 0x01, 0xAA])
    port = _FakePort(data)
    protocol = CresnetProtocol()
    raw_queue: queue.Queue[int] = queue.Queue()
    reader = SerialReader(port, protocol, raw_queue=raw_queue)

    reader.start()
    deadline = time.time() + 1
    while raw_queue.qsize() < len(data) and time.time() < deadline:
        time.sleep(0.005)
    reader.stop()

    seen = [raw_queue.get_nowait() for _ in range(raw_queue.qsize())]
    assert bytes(seen) == data


def test_no_raw_queue_by_default() -> None:
    """Existing two-positional-arg construction (no raw_queue) must keep
    working unchanged - SerialReader's original callers never pass one."""
    port = _FakePort(bytes([0x00, 0x05, 0x00]))
    protocol = CresnetProtocol()
    reader = SerialReader(port, protocol)

    reader.start()
    event = reader.events.get(timeout=1)
    reader.stop()

    assert event == PollTick(cycle=1)  # unaffected by the raw_queue addition
