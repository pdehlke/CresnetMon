"""Tests for the tkinter/ttk UI shell (cresnetmon.ui).

Requires a real display; each test skips itself if Tk can't initialize
(e.g. a headless runner).
"""

import tkinter as tk
from collections.abc import Iterator

import pytest

from cresnetmon.serial_io import PortInfo
from cresnetmon.ui import CresnetMonWindow, LabelDialog


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        window_root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    window_root.withdraw()  # keep it off-screen during tests
    yield window_root
    window_root.destroy()


def test_window_builds_expected_widgets(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])

    window = CresnetMonWindow(root)

    assert window.start_button["text"] == "Start"
    assert window.results["columns"] == ("cycle", "time", "dev", "sent", "received")


def test_refresh_ports_populates_combobox(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cresnetmon.ui.list_ports",
        lambda: [PortInfo(device="/dev/cu.usbserial-X", description="FT232R")],
    )

    window = CresnetMonWindow(root)

    assert window.port_combo["values"] == ("/dev/cu.usbserial-X",)
    assert window.port_var.get() == "/dev/cu.usbserial-X"


def test_set_running_toggles_button_and_disables_inputs(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    window = CresnetMonWindow(root)

    window.set_running(running=True)
    assert window.start_button["text"] == "Stop"
    assert str(window.device_id_entry["state"]) == "disabled"

    window.set_running(running=False)
    assert window.start_button["text"] == "Start"
    assert str(window.device_id_entry["state"]) == "normal"


def test_arm_button_starts_disabled_and_follows_running_state(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    window = CresnetMonWindow(root)

    assert str(window.arm_button["state"]) == "disabled"

    window.set_running(running=True)
    assert str(window.arm_button["state"]) == "normal"

    window.set_running(running=False)
    assert str(window.arm_button["state"]) == "disabled"


def test_set_armed_toggles_arm_button_text(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    window = CresnetMonWindow(root)

    window.set_armed(armed=True)
    assert window.arm_button["text"] == "Disarm"

    window.set_armed(armed=False)
    assert window.arm_button["text"] == "Arm"


def test_arm_disarm_callback_invoked(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    calls = []
    window = CresnetMonWindow(root, on_arm_disarm=lambda: calls.append(1))
    window.set_running(running=True)  # Arm is disabled (see test above) until running

    window.arm_button.invoke()

    assert calls == [1]


def test_add_row_and_clear_rows(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    window = CresnetMonWindow(root)

    window.add_row(1, "12:00:00", "05", "", "11 22 33")
    assert len(window.results.get_children()) == 1

    window.clear_rows()
    assert len(window.results.get_children()) == 0


def test_set_status_updates_label_text(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    window = CresnetMonWindow(root)

    window.set_status(42)

    assert window.status_var.get() == "Polling count: 42"


def test_start_stop_and_clear_callbacks_invoked(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    calls = {"start_stop": 0, "clear": 0}
    window = CresnetMonWindow(
        root,
        on_start_stop=lambda: calls.__setitem__("start_stop", calls["start_stop"] + 1),
        on_clear=lambda: calls.__setitem__("clear", calls["clear"] + 1),
    )

    window.start_button.invoke()
    window.clear_button.invoke()

    assert calls == {"start_stop": 1, "clear": 1}


def test_label_dialog_submit_passes_device_value_not_label(root: tk.Tk) -> None:
    submitted = []
    dialog = LabelDialog(
        root,
        device_options=[("67", "67 Foyer keypad"), ("70", "70 Garage dimmer")],
        default_label="67 Foyer keypad",
        on_submit=lambda device, button, note: submitted.append((device, button, note)),
        on_cancel=lambda: None,
    )
    assert dialog.device_var.get() == "67 Foyer keypad"

    dialog.button_var.set("dim up")
    dialog.note_var.set("Foyer cans to 100%")
    top = dialog.top
    dialog.submit_button.invoke()

    assert submitted == [("67", "dim up", "Foyer cans to 100%")]
    assert not top.winfo_exists()


def test_label_dialog_cancel_invokes_callback_without_submit(root: tk.Tk) -> None:
    submitted = []
    cancelled = []
    dialog = LabelDialog(
        root,
        device_options=[("67", "67 Foyer keypad")],
        default_label="67 Foyer keypad",
        on_submit=lambda device, button, note: submitted.append((device, button, note)),
        on_cancel=lambda: cancelled.append(1),
    )
    top = dialog.top

    # WM_DELETE_WINDOW is bound to _cancel (module docstring: closing the
    # window counts as Cancel); calling it directly exercises the same
    # path a real close-button click would take.
    dialog._cancel()

    assert submitted == []
    assert cancelled == [1]
    assert not top.winfo_exists()


def test_label_dialog_unlisted_device_falls_back_to_label_as_value(root: tk.Tk) -> None:
    """A device id present in the burst but absent from the seed map is
    passed through as both value and label (app.py's _device_options)."""
    submitted = []
    dialog = LabelDialog(
        root,
        device_options=[("99", "99")],
        default_label="99",
        on_submit=lambda device, button, note: submitted.append(device),
        on_cancel=lambda: None,
    )

    dialog.submit_button.invoke()

    assert submitted == ["99"]
