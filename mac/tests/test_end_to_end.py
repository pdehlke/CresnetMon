"""End-to-end test: a known byte sequence through the real stack
(SerialReader's background thread -> CresnetProtocol -> CresnetMonApp's
tk.after() polling -> Treeview rows), diffed against expected output.

Unlike the per-module tests elsewhere, this drives everything through
CresnetMonApp exactly as main.py does - only the serial port itself is
faked, so this is the closest thing to "run the real app against a known
recording" available without real hardware.

Requires a real display; skips itself if Tk can't initialize.
"""

import re
import time
import tkinter as tk
from collections.abc import Iterator

import pytest

from cresnetmon.app import CresnetMonApp
from cresnetmon.config import Settings
from cresnetmon.protocol import MASTER_ADDR

DEVICE_A = 0x05
DEVICE_B = 0x07
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")

# A short bus recording: sync, two poll cycles from A (the poll reference,
# set by the first empty frame seen), a message A -> master, a poll from B
# (not the reference, no tick - but it does become the new "last non-master
# address", which is what a message-to-master reports as its device id; see
# protocol.py's _finish_message), a message master -> B, then one more A
# poll. Device A's message is placed right after A's own poll rather than
# after B's, specifically so send-id attribution isn't ambiguous. See
# STRATEGY.md's protocol notes for the state machine this walks.
SEQUENCE = bytes(
    [
        0x00,  # sync -> Ready
        DEVICE_A, 0x00,  # poll A -> tick, cycle 1, A becomes the reference
        DEVICE_A, 0x00,  # poll A -> tick, cycle 2
        MASTER_ADDR, 0x03, 0x11, 0x22, 0x33,  # message A -> master
        DEVICE_B, 0x00,  # poll B -> no tick (not the reference)
        DEVICE_B, 0x02, 0xAA, 0xBB,  # message master -> B
        DEVICE_A, 0x00,  # poll A -> tick, cycle 3
    ]
)  # fmt: skip

EXPECTED_ROWS_UNFILTERED = [
    ("2", "05", "", "11 22 33"),
    ("2", "07", "AA BB", ""),
]


class _FakePort:
    """Feeds SEQUENCE, then idles until closed - same shape as the other
    test files' fakes."""

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


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        window_root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    window_root.withdraw()
    yield window_root
    window_root.destroy()


def _run_sequence(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch, *, device_filter: str, expected_row_count: int
) -> CresnetMonApp:
    monkeypatch.setattr("cresnetmon.ui.list_ports", lambda: [])
    monkeypatch.setattr("cresnetmon.config.load", Settings)
    fake_port = _FakePort(SEQUENCE)
    monkeypatch.setattr("cresnetmon.app.open_port", lambda device: fake_port)

    app = CresnetMonApp(root)
    app.window.device_id_var.set(device_filter)
    app.window.start_button.invoke()

    deadline = time.time() + 2
    while len(app.window.results.get_children()) < expected_row_count and time.time() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()  # one more pump so the final status tick lands too

    app.window.start_button.invoke()  # stop
    return app


def test_full_sequence_unfiltered_matches_expected_rows(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _run_sequence(root, monkeypatch, device_filter="", expected_row_count=2)

    rows = app.window.results.get_children()
    actual = [app.window.results.item(row, "values") for row in rows]

    assert len(actual) == len(EXPECTED_ROWS_UNFILTERED)
    for (cycle, time_str, dev, sent, received), expected in zip(
        actual, EXPECTED_ROWS_UNFILTERED, strict=True
    ):
        assert TIME_RE.match(time_str) is not None
        assert (cycle, dev, sent, received) == expected
    assert app.window.status_var.get() == "Polling count: 3"


def test_full_sequence_filtered_to_device_b_shows_one_row(
    root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _run_sequence(root, monkeypatch, device_filter="07", expected_row_count=1)

    rows = app.window.results.get_children()
    actual = [app.window.results.item(row, "values") for row in rows]

    assert len(actual) == 1
    cycle, _time, dev, sent, received = actual[0]
    assert (cycle, dev, sent, received) == ("2", "07", "AA BB", "")
    # Ticks are never filtered by device id - status still reflects all 3.
    assert app.window.status_var.get() == "Polling count: 3"
