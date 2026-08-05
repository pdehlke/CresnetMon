"""Tests for cresnetmon.devices: seed-map loading and label formatting.
Uses tmp_path so nothing depends on the real vendored seed file's exact
contents (that file is checked separately, below)."""

import json
from pathlib import Path

from cresnetmon.devices import SEED_PATH, DeviceInfo, format_device_label, load_seed


def _write_seed(tmp_path: Path, devices: list[dict[str, str]]) -> Path:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"source": "test", "devices": devices}))
    return path


def test_load_seed_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    assert load_seed(tmp_path / "does-not-exist.json") == {}


def test_load_seed_unreadable_json_returns_empty_dict(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text("not json{")

    assert load_seed(path) == {}


def test_load_seed_parses_hex_ids(tmp_path: Path) -> None:
    path = _write_seed(
        tmp_path,
        [{"id": "0x67", "model": "CNX-B8", "room": "Foyer"}],
    )

    devices = load_seed(path)

    assert devices == {0x67: DeviceInfo(model="CNX-B8", room="Foyer")}


def test_load_seed_skips_malformed_entries(tmp_path: Path) -> None:
    path = _write_seed(
        tmp_path,
        [
            {"id": "0x67", "model": "CNX-B8", "room": "Foyer"},
            {"model": "no id field"},
            {"id": "not-hex", "model": "bad id"},
        ],
    )

    devices = load_seed(path)

    assert devices == {0x67: DeviceInfo(model="CNX-B8", room="Foyer")}


def test_load_seed_missing_room_is_none(tmp_path: Path) -> None:
    path = _write_seed(tmp_path, [{"id": "0x0A", "model": "ST-IO"}])

    devices = load_seed(path)

    assert devices[0x0A].room is None


def test_format_device_label_unknown_id_falls_back_to_hex() -> None:
    assert format_device_label(0x05, {}) == "05"


def test_format_device_label_known_model_and_room() -> None:
    devices = {0x67: DeviceInfo(model="CNX-B8", room="Foyer")}

    assert format_device_label(0x67, devices) == "67 Foyer keypad"


def test_format_device_label_known_model_no_room() -> None:
    devices = {0x0A: DeviceInfo(model="ST-IO", room=None)}

    assert format_device_label(0x0A, devices) == "0A I/O module"


def test_format_device_label_unmapped_model_falls_back_to_model_string() -> None:
    devices = {0x99: DeviceInfo(model="XYZ-9000", room="Attic")}

    assert format_device_label(0x99, devices) == "99 Attic XYZ-9000"


def test_real_vendored_seed_file_loads_and_covers_known_devices() -> None:
    """Sanity-checks the actual mac/seed/devices.json, not a fixture."""
    devices = load_seed(SEED_PATH)

    assert len(devices) == 18
    assert devices[0x67] == DeviceInfo(model="CNX-B8", room="Foyer")
    assert devices[0x74] == DeviceInfo(model="CLX-4HSW4", room="Garage")
    assert format_device_label(0x67, devices) == "67 Foyer keypad"
