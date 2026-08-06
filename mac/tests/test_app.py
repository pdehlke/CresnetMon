"""Tests for cresnetmon.app.CresnetMonApp: UI<->serial+protocol wiring.

Requires a real display; each test skips itself if Tk can't initialize.
Uses a fake serial port (no real hardware) and monkeypatches open_port and
the error-dialog call so nothing blocks on real widgets/dialogs.
"""

import json
import time
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from cresnetmon import config as config_module
from cresnetmon.app import APP_TITLE, CresnetMonApp, DeviceIdError, parse_device_id
from cresnetmon.capture import CaptureWriter
from cresnetmon.protocol import CresnetProtocol, Message, PollTick
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


def _make_running_armable_app(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> CresnetMonApp:
    """CresnetMonApp with running=True but no real reader/serial port -
    Arm/burst/dialog logic doesn't need one, only _running being true."""
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    app = CresnetMonApp(root)
    app._running = True
    app.window.set_running(running=True)
    return app


def test_arm_disarm_toggles_state_button_and_resets_burst(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_running_armable_app(root, monkeypatch)
    app._burst.feed(Message(cycle=1, text="AA", dev_id=0x05, to_master=True), now=0.0)
    assert app._burst.is_open is True

    app.window.arm_button.invoke()  # arm
    assert app._armed is True
    assert app.window.arm_button["text"] == "Disarm"
    assert app._burst.is_open is False  # arming resets any stale burst

    app.window.arm_button.invoke()  # disarm
    assert app._armed is False
    assert app.window.arm_button["text"] == "Arm"


def test_poll_tick_while_armed_does_not_open_a_burst(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_running_armable_app(root, monkeypatch)
    app._on_arm_disarm()  # arm

    app._handle_event(PollTick(cycle=1))

    assert app._burst.is_open is False


def test_burst_silence_timeout_opens_dialog_with_expected_defaults(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialogs: list[SimpleNamespace] = []

    def fake_dialog(
        parent: object,
        *,
        device_options: object,
        default_label: object,
        on_submit: object,
        on_cancel: object,
    ) -> None:
        dialogs.append(
            SimpleNamespace(
                parent=parent,
                device_options=device_options,
                default_label=default_label,
                on_submit=on_submit,
                on_cancel=on_cancel,
            )
        )

    monkeypatch.setattr("cresnetmon.app.LabelDialog", fake_dialog)
    app = _make_running_armable_app(root, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("cresnetmon.app.time.monotonic", lambda: clock[0])
    app._on_arm_disarm()  # arm

    protocol = CresnetProtocol()
    for byte in bytes([0x00, 0x67, 0x03, 0x11, 0x22, 0x33]):  # message from seeded device 0x67
        event = protocol.feed(byte)
        if event is not None:
            app._handle_event(event)

    assert dialogs == []  # not yet past the silence window
    clock[0] += 0.6
    app._check_burst()

    assert len(dialogs) == 1
    assert dialogs[0].default_label == "67 Foyer keypad"
    assert ("67", "67 Foyer keypad") in dialogs[0].device_options
    assert app._armed is False  # paused while the dialog is "open"
    assert app.window.arm_button["text"] == "Arm"


def test_dialog_submit_calls_handler_and_rearms(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialogs: list[SimpleNamespace] = []
    monkeypatch.setattr(
        "cresnetmon.app.LabelDialog",
        lambda parent, **kwargs: dialogs.append(SimpleNamespace(**kwargs)),
    )
    submitted: list[tuple[object, str, str, str]] = []
    monkeypatch.setattr(
        CresnetMonApp,
        "_on_label_submitted",
        lambda self, burst, started_at, closed_at, device, button, note: submitted.append(
            (burst, device, button, note)
        ),
    )
    app = _make_running_armable_app(root, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr("cresnetmon.app.time.monotonic", lambda: clock[0])
    app._on_arm_disarm()
    protocol = CresnetProtocol()
    for byte in bytes([0x00, 0x67, 0x03, 0x11, 0x22, 0x33]):
        event = protocol.feed(byte)
        if event is not None:
            app._handle_event(event)
    clock[0] += 0.6
    app._check_burst()
    dialog = dialogs[0]

    dialog.on_submit("67", "dim up", "Foyer cans to 100%")

    assert len(submitted) == 1
    burst, device, button, note = submitted[0]
    assert (device, button, note) == ("67", "dim up", "Foyer cans to 100%")
    assert burst.messages[0].text == "11 22 33"
    assert app._armed is True  # auto-rearmed
    assert app.window.arm_button["text"] == "Disarm"


def test_dialog_cancel_rearms_without_calling_handler(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialogs: list[SimpleNamespace] = []
    monkeypatch.setattr(
        "cresnetmon.app.LabelDialog",
        lambda parent, **kwargs: dialogs.append(SimpleNamespace(**kwargs)),
    )
    submitted: list[object] = []
    monkeypatch.setattr(CresnetMonApp, "_on_label_submitted", lambda self, *a: submitted.append(a))
    app = _make_running_armable_app(root, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr("cresnetmon.app.time.monotonic", lambda: clock[0])
    app._on_arm_disarm()
    protocol = CresnetProtocol()
    for byte in bytes([0x00, 0x67, 0x03, 0x11, 0x22, 0x33]):
        event = protocol.feed(byte)
        if event is not None:
            app._handle_event(event)
    clock[0] += 0.6
    app._check_burst()

    dialogs[0].on_cancel()

    assert submitted == []
    assert app._armed is True  # still auto-rearms on cancel


def test_rearm_is_noop_if_monitoring_stopped_meanwhile(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_running_armable_app(root, monkeypatch)
    app._on_arm_disarm()
    app._armed = False  # simulate a dialog having just paused arming
    app._running = False  # ...and Stop was clicked while it was open

    app._rearm()

    assert app._armed is False


def test_stop_disarms_and_discards_open_burst(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_running_armable_app(root, monkeypatch)
    app._on_arm_disarm()
    app._burst.feed(Message(cycle=1, text="AA", dev_id=0x05, to_master=True), now=0.0)
    assert app._burst.is_open is True

    app._stop()

    assert app._armed is False
    assert app._burst.is_open is False


def test_full_arm_to_submit_flow_writes_a_real_capture_file(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end for the labeling mode's write path: real CaptureWriter
    (directory redirected to tmp_path), real _on_label_submitted, real
    device-info lookup from the seed map - only the dialog's own widget
    mechanics are stubbed (LabelDialog itself is covered directly in
    test_ui.py)."""
    monkeypatch.setattr("cresnetmon.app.CaptureWriter", lambda: CaptureWriter(tmp_path))
    dialogs: list[SimpleNamespace] = []
    monkeypatch.setattr(
        "cresnetmon.app.LabelDialog",
        lambda parent, **kwargs: dialogs.append(SimpleNamespace(**kwargs)),
    )
    app = _make_running_armable_app(root, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr("cresnetmon.app.time.monotonic", lambda: clock[0])
    app._on_arm_disarm()

    protocol = CresnetProtocol()
    for byte in bytes([0x00, 0x67, 0x03, 0x11, 0x22, 0x33]):  # known seed device 0x67
        event = protocol.feed(byte)
        if event is not None:
            app._handle_event(event)
    clock[0] += 0.6
    app._check_burst()

    assert list(tmp_path.glob("*.jsonl")) == []

    dialogs[0].on_submit("67", "dim up", "Foyer cans to 100%")

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text().splitlines()[0])
    assert record["device"] == {"id": "0x67", "model": "CNX-B8", "room": "Foyer"}
    assert record["button"] == "dim up"
    assert record["note"] == "Foyer cans to 100%"
    assert record["frames"] == [
        {"dev_id": "0x67", "cycle": 0, "text": "11 22 33", "to_master": False}
    ]
    assert app._armed is True  # auto-rearmed after a real submit
    assert app.window.arm_button["text"] == "Disarm"
