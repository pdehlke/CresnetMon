"""Wires the UI shell to the serial/protocol layers.

The only module that knows about all three of ui.py, serial_io.py, and
protocol.py: start/stop handling, device-id filter parsing, error dialogs,
and draining the SerialReader's event queue into Treeview rows via
tk.after() polling (tkinter isn't thread-safe; the reader thread only ever
touches the queue).
"""

import tkinter as tk
from datetime import datetime
from tkinter import messagebox

from cresnetmon.protocol import CresnetProtocol, Message, PollTick, ProtocolEvent
from cresnetmon.serial_io import PortOpenError, SerialReader, open_port
from cresnetmon.ui import CresnetMonWindow

APP_TITLE = "Cresnet Monitor"
POLL_INTERVAL_MS = 50


class DeviceIdError(ValueError):
    """Raised when the device-id filter field isn't valid hex (00-FF)."""


def parse_device_id(text: str) -> int:
    """Parse the device-id filter field. Blank or '0' means all devices,
    matching MainForm.cs:273-280's byte.TryParse(AllowHexSpecifier)."""
    text = text.strip()
    if not text:
        return 0
    try:
        value = int(text, 16)
    except ValueError:
        raise DeviceIdError(f"'{text}' is not a valid hex device ID") from None
    if not 0 <= value <= 0xFF:
        raise DeviceIdError(f"'{text}' is out of range (00-FF)")
    return value


class CresnetMonApp:
    """Owns run state; connects UI button events to the serial reader and
    protocol parser, and the parser's events back to the UI."""

    def __init__(self, root: tk.Tk) -> None:
        self._protocol = CresnetProtocol()
        self._reader: SerialReader | None = None
        self._device_filter = 0
        self._running = False

        self.window = CresnetMonWindow(
            root,
            on_start_stop=self._on_start_stop,
            on_clear=self._on_clear,
        )

    def _on_start_stop(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        """Mirrors btnStart_Click's start branch (MainForm.cs:273-289):
        validate the device-id filter, open the port, reset parse state,
        launch the reader. Unlike the original, a failed port open shows an
        error dialog instead of failing silently (STRATEGY.md task 5)."""
        try:
            self._device_filter = parse_device_id(self.window.device_id_var.get())
        except DeviceIdError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        try:
            port = open_port(self.window.port_var.get())
        except PortOpenError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self._protocol.start()
        self._reader = SerialReader(port, self._protocol)
        self._reader.start()
        self._running = True
        self.window.set_running(running=True)
        self._poll_events()

    def _stop(self) -> None:
        """Mirrors btnStart_Click's stop branch (MainForm.cs:265-270)."""
        if self._reader is not None:
            self._drain_events(self._reader)  # pick up any last events
            self._reader.stop()
            self._reader = None
        self._running = False
        self.window.set_running(running=False)

    def _on_clear(self) -> None:
        """Mirrors btnClear_Click (MainForm.cs:292-298): counter always
        resets, poll reference only resets while stopped."""
        self.window.clear_rows()
        self._protocol.clear_counts(keep_poll_reference=self._running)
        self.window.set_status(0)

    def _poll_events(self) -> None:
        if self._reader is not None:
            self._drain_events(self._reader)
        if self._running:
            self.window.root.after(POLL_INTERVAL_MS, self._poll_events)

    def _drain_events(self, reader: SerialReader) -> None:
        while not reader.events.empty():
            self._handle_event(reader.events.get_nowait())

    def _handle_event(self, event: ProtocolEvent) -> None:
        if isinstance(event, PollTick):
            self.window.set_status(event.cycle)
            return
        self._handle_message(event)

    def _handle_message(self, event: Message) -> None:
        """Mirrors ShowMessage's device-id filter (MainForm.cs:211-223).
        Unlike PollTick, a Message does not refresh the status text on its
        own in the original - only its row gets added."""
        if self._device_filter != 0 and event.dev_id != self._device_filter:
            return
        time_str = datetime.now().strftime("%H:%M:%S")
        dev_str = f"{event.dev_id:02X}"
        sent = "" if event.to_master else event.text
        received = event.text if event.to_master else ""
        self.window.add_row(event.cycle, time_str, dev_str, sent, received)
