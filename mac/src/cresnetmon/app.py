"""Wires the UI shell to the serial/protocol/config/burst/capture layers.

The only module that knows about ui.py, serial_io.py, protocol.py,
config.py, devices.py, burst.py, and capture.py together: start/stop
handling, device-id filter parsing, error dialogs, draining the
SerialReader's event queue into Treeview rows via tk.after() polling
(tkinter isn't thread-safe; the reader thread only ever touches the
queue), restoring/persisting window geometry plus last-used port/
device-id across launches, and the labeling/capture mode's arm/burst/
dialog/write flow (STRATEGY.md's "Labeling / capture mode" section).
"""

import time
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from tkinter import messagebox

from cresnetmon import config
from cresnetmon.burst import Burst, BurstGrouper
from cresnetmon.capture import CaptureWriter
from cresnetmon.devices import format_device_label, load_seed
from cresnetmon.protocol import CresnetProtocol, Message, PollTick, ProtocolEvent
from cresnetmon.serial_io import PortOpenError, SerialReader, open_port
from cresnetmon.ui import CresnetMonWindow, LabelDialog

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
        self._settings = config.load()
        self._devices = load_seed()
        self._burst = BurstGrouper()
        self._armed = False
        self._burst_started_wall: datetime | None = None
        self._capture_writer: CaptureWriter | None = None

        self.window = CresnetMonWindow(
            root,
            on_start_stop=self._on_start_stop,
            on_clear=self._on_clear,
            on_arm_disarm=self._on_arm_disarm,
            initial_port=self._settings.com_port,
            initial_device_id=self._settings.device_id,
        )
        # Applied after CresnetMonWindow's own default-geometry call, so a
        # saved size/position wins over the built-in 640x400 default.
        config.apply_to_window(self._settings, root)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        """Mirrors MainForm_FormClosing (MainForm.cs:247-256): close the
        port if running, then persist geometry plus the current port and
        device-id fields."""
        if self._reader is not None:
            self._reader.stop()
            self._reader = None
        settings = config.capture_from_window(self._settings, self.window.root)
        settings = replace(
            settings,
            device_id=self.window.device_id_var.get(),
            com_port=self.window.port_var.get(),
        )
        config.save(settings)
        self.window.root.destroy()

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
        """Mirrors btnStart_Click's stop branch (MainForm.cs:265-270).
        Also disarms - labeling needs a live bus, and any burst already in
        progress is discarded rather than left to time out later."""
        if self._reader is not None:
            self._drain_events(self._reader)  # pick up any last events
            self._reader.stop()
            self._reader = None
        self._running = False
        self.window.set_running(running=False)
        self._armed = False
        self._burst.reset()
        self.window.set_armed(armed=False)

    def _on_clear(self) -> None:
        """Mirrors btnClear_Click (MainForm.cs:292-298): counter always
        resets, poll reference only resets while stopped."""
        self.window.clear_rows()
        self._protocol.clear_counts(keep_poll_reference=self._running)
        self.window.set_status(0)

    def _poll_events(self) -> None:
        if self._reader is not None:
            self._drain_events(self._reader)
        if self._armed:
            self._check_burst()
        if self._running:
            self.window.root.after(POLL_INTERVAL_MS, self._poll_events)

    def _drain_events(self, reader: SerialReader) -> None:
        while not reader.events.empty():
            self._handle_event(reader.events.get_nowait())

    def _handle_event(self, event: ProtocolEvent) -> None:
        if self._armed:
            was_open = self._burst.is_open
            self._burst.feed(event, time.monotonic())
            if not was_open and self._burst.is_open:
                self._burst_started_wall = datetime.now()
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
        dev_str = format_device_label(event.dev_id, self._devices)
        sent = "" if event.to_master else event.text
        received = event.text if event.to_master else ""
        self.window.add_row(event.cycle, time_str, dev_str, sent, received)

    def _on_arm_disarm(self) -> None:
        self._armed = not self._armed
        self._burst.reset()
        self.window.set_armed(armed=self._armed)

    def _check_burst(self) -> None:
        burst = self._burst.check(time.monotonic())
        if burst is None:
            return
        closed_at = datetime.now()
        started_at = self._burst_started_wall or closed_at
        self._armed = False
        self.window.set_armed(armed=False)
        self._show_label_dialog(burst, started_at, closed_at)

    def _device_options(self, burst: Burst) -> list[tuple[str, str]]:
        """(hex-id, display-label) pairs for the label dialog's device
        dropdown: every seeded device, plus any device id that showed up
        in this burst but isn't in the seed (shown as plain hex so it's
        still selectable/identifiable)."""
        options = [
            (f"{dev_id:02X}", format_device_label(dev_id, self._devices))
            for dev_id in sorted(self._devices)
        ]
        known_ids = {value for value, _label in options}
        for message in burst.messages:
            dev_id_str = f"{message.dev_id:02X}"
            if dev_id_str not in known_ids:
                options.append((dev_id_str, dev_id_str))
                known_ids.add(dev_id_str)
        return options

    def _show_label_dialog(self, burst: Burst, started_at: datetime, closed_at: datetime) -> None:
        """Prompts for the burst's label, per STRATEGY.md's "Label schema".
        Default device selection is whichever device id appeared first in
        the burst, matching the design doc."""
        options = self._device_options(burst)
        by_value = dict(options)
        default_value = f"{burst.messages[0].dev_id:02X}" if burst.messages else ""
        default_label = by_value.get(default_value, default_value)

        def on_submit(device_value: str, button_text: str, note: str) -> None:
            self._on_label_submitted(burst, started_at, closed_at, device_value, button_text, note)
            self._rearm()

        LabelDialog(
            self.window.root,
            device_options=options,
            default_label=default_label,
            on_submit=on_submit,
            on_cancel=self._rearm,
        )

    def _rearm(self) -> None:
        """Auto-rearm after a label is submitted or cancelled, unless
        monitoring has since been stopped (STRATEGY.md: submitting
        auto-rearms; Stop always disarms - see _stop())."""
        if not self._running:
            return
        self._armed = True
        self.window.set_armed(armed=True)

    def _on_label_submitted(
        self,
        burst: Burst,
        started_at: datetime,
        closed_at: datetime,
        device_value: str,
        button_text: str,
        note: str,
    ) -> None:
        """Writes the labeled burst to this session's JSONL capture file
        (mac/captures/*.jsonl), per STRATEGY.md's "Output" section."""
        info = self._devices.get(int(device_value, 16))
        device = {
            "id": f"0x{device_value}",
            "model": info.model if info else None,
            "room": info.room if info else None,
        }
        if self._capture_writer is None:
            self._capture_writer = CaptureWriter()
        self._capture_writer.write(
            burst,
            started_at=started_at,
            closed_at=closed_at,
            device=device,
            button=button_text,
            note=note,
        )
