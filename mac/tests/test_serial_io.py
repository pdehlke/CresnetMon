"""Tests for cresnetmon.serial_io.SerialReader using a fake port (no real
serial hardware needed)."""

import time

from cresnetmon.protocol import CresnetProtocol, PollTick
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
