"""Serial I/O for the Cresnet bus: port enumeration, opening, and a
background reader thread that feeds bytes into a CresnetProtocol and hands
resulting events back via a thread-safe queue.

No UI code here; see ui.py (tasks 4-5) for how events get displayed. Mirrors
the read loop in MainForm.cs:141-208 (CresNetProcessByte) and the port-open
logic in MainForm.cs:86-99 (OpenPort), split out as pure I/O plumbing around
the already-ported protocol.CresnetProtocol.

This is also where the two wall-clock reads protocol.py deliberately can't
do live: attaching `Message.read_at` and (optionally) mirroring every raw
byte onto `raw_queue` for the raw byte-stream log (STRATEGY.md task 14).
Both happen right here, at the moment a byte is actually read off the wire,
rather than later at queue-drain time in app.py - that avoids smearing
timestamps to app.py's 50ms polling cadence.
"""

import queue
import threading
import time
from dataclasses import dataclass, replace
from typing import Protocol

import serial
from serial.tools import list_ports as _list_ports

from cresnetmon.protocol import CresnetProtocol, Message, ProtocolEvent

BAUD_RATE = 38400  # matches MainForm.cs:90; Cresnet's fixed bus speed


class CresnetMonError(Exception):
    """Base exception for cresnetmon."""


class PortOpenError(CresnetMonError):
    """Raised when the serial port could not be opened."""


@dataclass(frozen=True, slots=True)
class PortInfo:
    """A discovered serial port."""

    device: str
    description: str


def list_ports() -> list[PortInfo]:
    """Enumerate available serial ports.

    On macOS this surfaces USB-RS485 adapters as /dev/cu.usbserial-* (or
    /dev/tty.usbserial-*); there is no SerialPort.GetPortNames() equivalent,
    pyserial's comports() is the cross-platform stand-in (MainForm.cs:78).
    """
    return [PortInfo(device=p.device, description=p.description) for p in _list_ports.comports()]


def open_port(device: str, *, baudrate: int = BAUD_RATE) -> serial.Serial:
    """Open a serial port at the given baud rate (default: Cresnet's fixed
    38400). Raises PortOpenError on failure, mirroring OpenPort's try/except
    (MainForm.cs:86-99) instead of returning a bool + out-param."""
    try:
        return serial.Serial(device, baudrate)
    except serial.SerialException as exc:
        raise PortOpenError(f"failed to open {device}: {exc}") from exc


class SerialPort(Protocol):
    """Structural interface SerialReader needs from a port object.

    serial.Serial satisfies this. Tests use a lightweight fake instead of
    talking to real hardware.
    """

    is_open: bool

    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


class SerialReader:
    """Reads bytes from an open serial port on a background thread, feeds
    them into a CresnetProtocol, and puts any resulting events on a queue.

    The protocol instance is owned by the caller, not created here, so
    start/clear semantics (CresnetProtocol.start()/clear_counts()) stay the
    caller's responsibility - mirrors MainForm's button handlers owning
    those calls around the read loop rather than the loop owning them.

    `raw_queue`, if given, receives every raw byte read (as an int 0-255),
    independent of whatever protocol.py does with it - including bytes that
    are part of routine polling, which protocol.py mostly discards. This is
    the raw byte-stream log's tap point (STRATEGY.md task 14); app.py owns
    deciding whether to pass one in (the raw-log toggle) and draining it.
    `None` (the default) costs nothing extra per byte beyond the `is None`
    check - existing callers that don't pass it are unaffected.
    """

    def __init__(
        self,
        port: SerialPort,
        protocol: CresnetProtocol,
        *,
        raw_queue: queue.Queue[int] | None = None,
    ) -> None:
        self._port = port
        self._protocol = protocol
        self._events: queue.Queue[ProtocolEvent] = queue.Queue()
        self._raw_queue = raw_queue
        self._thread: threading.Thread | None = None

    @property
    def events(self) -> queue.Queue[ProtocolEvent]:
        """Thread-safe queue of events; poll from the UI thread."""
        return self._events

    def start(self) -> None:
        """Start the background read loop (MainForm.cs:288). No-op if
        already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Close the port, ending the read loop (MainForm.cs:267,
        m_serCresnet.Close() from btnStart_Click's stop branch)."""
        self._port.close()

    def _run(self) -> None:
        while self._port.is_open:
            try:
                data = self._port.read(1)
            except serial.SerialException:
                # Port closed out from under a blocked read - normal
                # shutdown path, matches MainForm.cs:203-207.
                return
            if not data:
                continue
            byte = data[0]
            if self._raw_queue is not None:
                self._raw_queue.put(byte)
            event = self._protocol.feed(byte)
            if event is not None:
                if isinstance(event, Message):
                    event = replace(event, read_at=time.time())
                self._events.put(event)
