"""Tests for cresnetmon.app.CresnetMonApp: UI<->serial+protocol wiring.

Requires a real display; each test skips itself if Tk can't initialize.
Uses a fake serial port (no real hardware) and monkeypatches open_port and
the error-dialog call so nothing blocks on real widgets/dialogs.
"""

import time
import tkinter as tk
from collections.abc import Iterator

import pytest

from cresnetmon import config as config_module
from cresnetmon.app import APP_TITLE, CresnetMonApp, DeviceIdError, parse_device_id
from cresnetmon.protocol import CresnetProtocol
from cresnetmon.serial_io import SerialReader


class _FakePort:
    """Same shape as tests/test_serial_io.py's fake: feeds a fixed byte
    sequence, then idles until closed."""

    def __init__(self, data: bytes = b"") -> None:
        self.is_open = True
        self._data = bytearray(data)

    def read(self, size: int = 1) -> bytes:
        if not self.is_open or not self._data:
            time.sleep(0.005)
            return b""
        return bytes([self._data.pop(0)])

    def close(self) -> None:
        self.is_open = False


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        window_root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    window_root.withdraw()
    yield window_root
    window_root.destroy()


@pytest.fixture
def errors(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture messagebox.showerror calls instead of popping real dialogs."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "cresnetmon.app.messagebox.showerror",
        lambda title, message: calls.append((title, message)),
    )
    return calls


@pytest.mark.parametrize(("text", "expected"), [("", 0), ("0", 0), ("5", 0x05), ("ff", 0xFF)])
def test_parse_device_id_valid(text: str, expected: int) -> None:
    assert parse_device_id(text) == expected


@pytest.mark.parametrize("text", ["zz", "100", "-1"])
def test_parse_device_id_invalid(text: str) -> None:
    with pytest.raises(DeviceIdError):
        parse_device_id(text)


def test_start_with_bad_device_id_shows_error_and_does_not_run(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch, errors: list[tuple[str, str]]
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    app = CresnetMonApp(root)
    app.window.device_id_var.set("zz")

    app.window.start_button.invoke()

    assert errors == [(APP_TITLE, "'zz' is not a valid hex device ID")]
    assert app.window.start_button["text"] == "Start"


def test_start_with_bad_port_shows_error_and_does_not_run(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch, errors: list[tuple[str, str]]
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    app = CresnetMonApp(root)
    app.window.port_var.set("/dev/does-not-exist")

    app.window.start_button.invoke()

    assert len(errors) == 1
    assert errors[0][0] == APP_TITLE
    assert app.window.start_button["text"] == "Start"


def test_start_success_toggles_running_and_drains_events_into_rows(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    fake_port = _FakePort(bytes([0x00, 0x05, 0x00]))  # sync + empty (poll) frame
    monkeypatch.setattr("cresnetmon.app.open_port", lambda device: fake_port)

    app = CresnetMonApp(root)
    app.window.start_button.invoke()

    assert app.window.start_button["text"] == "Stop"
    # Let the reader thread produce the PollTick and one after()-poll run.
    deadline = time.time() + 1
    while app.window.status_var.get() == "Polling count: 0" and time.time() < deadline:
        root.update()
        time.sleep(0.01)

    assert app.window.status_var.get() == "Polling count: 1"

    app.window.start_button.invoke()  # stop
    assert app.window.start_button["text"] == "Start"
    assert fake_port.is_open is False


def test_message_event_filtered_by_device_id(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    app = CresnetMonApp(root)
    app._device_filter = 0x05

    protocol = CresnetProtocol()
    for byte in bytes([0x00, 0x07, 0x03, 0x11, 0x22, 0x33]):  # message from 0x07, not 0x05
        event = protocol.feed(byte)
        if event is not None:
            app._handle_event(event)

    assert len(app.window.results.get_children()) == 0


def test_message_event_matching_filter_adds_row(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    app = CresnetMonApp(root)
    app._device_filter = 0x05

    protocol = CresnetProtocol()
    for byte in bytes([0x00, 0x05, 0x03, 0x11, 0x22, 0x33]):
        event = protocol.feed(byte)
        if event is not None:
            app._handle_event(event)

    rows = app.window.results.get_children()
    assert len(rows) == 1
    assert app.window.results.item(rows[0], "values")[2] == "05"


def test_clear_resets_counter_and_keeps_poll_reference_while_running(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    app = CresnetMonApp(root)
    app._running = True
    app._protocol.msg_count = 5

    app.window.clear_button.invoke()

    assert app._protocol.msg_count == 0
    assert app.window.status_var.get() == "Polling count: 0"
    assert len(app.window.results.get_children()) == 0


def test_app_restores_saved_port_and_device_id(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    monkeypatch.setattr(
        "cresnetmon.config.load",
        lambda: config_module.Settings(device_id="0A", com_port="/dev/cu.usbserial-X"),
    )

    app = CresnetMonApp(root)

    assert app.window.device_id_var.get() == "0A"
    assert app.window.port_var.get() == "/dev/cu.usbserial-X"


def test_on_close_persists_settings_and_stops_running_reader(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    monkeypatch.setattr("cresnetmon.config.load", lambda: config_module.Settings())
    saved: list[config_module.Settings] = []
    monkeypatch.setattr("cresnetmon.config.save", saved.append)
    monkeypatch.setattr(root, "destroy", lambda: None)

    app = CresnetMonApp(root)
    fake_port = _FakePort()
    app._reader = SerialReader(fake_port, app._protocol)
    app._reader.start()
    app._running = True
    app.window.device_id_var.set("0A")
    app.window.port_var.set("/dev/cu.usbserial-X")

    app._on_close()

    assert fake_port.is_open is False
    assert app._reader is None
    assert len(saved) == 1
    assert saved[0].device_id == "0A"
    assert saved[0].com_port == "/dev/cu.usbserial-X"
