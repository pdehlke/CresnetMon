"""Tests for cresnetmon.config: JSON persistence + window geometry
restore/capture. Uses tmp_path so nothing touches the real
~/Library/Application Support/CresnetMon path. Geometry tests need a real
display; each skips itself if Tk can't initialize.
"""

import tkinter as tk
from collections.abc import Iterator
from pathlib import Path

import pytest

from cresnetmon.config import Settings, apply_to_window, capture_from_window, load, save


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        window_root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    yield window_root
    window_root.destroy()


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    settings = load(tmp_path / "does-not-exist.json")
    assert settings == Settings()


def test_load_unreadable_json_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("not json{")

    assert load(path) == Settings()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    original = Settings(
        x=10,
        y=20,
        width=640,
        height=400,
        maximized=True,
        device_id="05",
        com_port="/dev/cu.usbserial-X",
    )

    save(original, path)
    loaded = load(path)

    assert loaded == original


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"width": 640, "height": 400, "bogus_field": "ignored"}')

    settings = load(path)

    assert settings.width == 640
    assert settings.height == 400


def test_apply_to_window_does_nothing_without_saved_geometry(root: tk.Tk) -> None:
    before = root.geometry()

    apply_to_window(Settings(), root)

    assert root.geometry() == before


def test_apply_then_capture_round_trips_geometry(root: tk.Tk) -> None:
    apply_to_window(Settings(x=50, y=60, width=500, height=300), root)
    root.update_idletasks()

    captured = capture_from_window(Settings(), root)

    assert captured.width == 500
    assert captured.height == 300
    assert captured.maximized is False


def test_capture_preserves_device_id_and_com_port(root: tk.Tk) -> None:
    existing = Settings(device_id="0A", com_port="/dev/cu.usbserial-X")

    captured = capture_from_window(existing, root)

    assert captured.device_id == "0A"
    assert captured.com_port == "/dev/cu.usbserial-X"
