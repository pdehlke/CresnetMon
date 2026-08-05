"""Tests for the tkinter/ttk UI shell (cresnetmon.ui).

Requires a real display; each test skips itself if Tk can't initialize
(e.g. a headless runner).
"""

import tkinter as tk
from collections.abc import Iterator

import pytest

from cresnetmon.serial_io import PortInfo
from cresnetmon.ui import CresnetMonWindow


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
