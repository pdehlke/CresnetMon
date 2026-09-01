"""Tests for cresnetmon.capture.CaptureWriter. Uses tmp_path so nothing
touches the real mac/captures/ directory."""

import json
from datetime import datetime
from pathlib import Path

from cresnetmon.burst import Burst
from cresnetmon.capture import CaptureWriter
from cresnetmon.protocol import Message

MSG_A = Message(
    cycle=118,
    text="11 22 33",
    dev_id=0x67,
    to_master=True,
    dest_id=0x02,
    raw=bytes([0x02, 0x03, 0x11, 0x22, 0x33]),
    read_at=1756642872.114,
)
MSG_B = Message(
    cycle=118,
    text="AA 01 64",
    dev_id=0x70,
    to_master=False,
    dest_id=0x70,
    raw=bytes([0x70, 0x03, 0xAA, 0x01, 0x64]),
    read_at=1756642872.201,
)


def test_writer_creates_captures_directory(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    assert not directory.exists()

    CaptureWriter(directory)

    assert directory.is_dir()


def test_writer_names_file_after_session_start(tmp_path: Path) -> None:
    writer = CaptureWriter(tmp_path)

    assert writer.path.parent == tmp_path
    assert writer.path.suffix == ".jsonl"
    # e.g. 20260805T164512.jsonl - just check it parses as a timestamp.
    datetime.strptime(writer.path.stem, "%Y%m%dT%H%M%S")


def test_write_appends_one_json_line_with_expected_shape(tmp_path: Path) -> None:
    writer = CaptureWriter(tmp_path)
    burst = Burst(messages=(MSG_A, MSG_B), opened_at=10.0, closed_at=10.5)
    started_at = datetime(2026, 8, 5, 15, 41, 12, 114000)
    closed_at = datetime(2026, 8, 5, 15, 41, 12, 640000)

    writer.write(
        burst,
        started_at=started_at,
        closed_at=closed_at,
        device={"id": "0x67", "model": "CNX-B8", "room": "Foyer"},
        button="button 3 (dim up)",
        note="Foyer cans should ramp to 100%",
    )

    lines = writer.path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "burst_started": started_at.isoformat(),
        "burst_closed": closed_at.isoformat(),
        "frames": [
            {
                "dev_id": "0x67",
                "cycle": 118,
                "text": "11 22 33",
                "to_master": True,
                "t": 1756642872.114,
                "dest_id": "0x02",
                "raw": "02 03 11 22 33",
            },
            {
                "dev_id": "0x70",
                "cycle": 118,
                "text": "AA 01 64",
                "to_master": False,
                "t": 1756642872.201,
                "dest_id": "0x70",
                "raw": "70 03 AA 01 64",
            },
        ],
        "device": {"id": "0x67", "model": "CNX-B8", "room": "Foyer"},
        "button": "button 3 (dim up)",
        "note": "Foyer cans should ramp to 100%",
    }


def test_write_appends_multiple_records_on_separate_lines(tmp_path: Path) -> None:
    writer = CaptureWriter(tmp_path)
    burst = Burst(messages=(MSG_A,), opened_at=0.0, closed_at=0.5)
    now = datetime.now()

    for i in range(3):
        writer.write(
            burst,
            started_at=now,
            closed_at=now,
            device={"id": "0x67", "model": None, "room": None},
            button=f"press {i}",
            note="",
        )

    lines = writer.path.read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["button"] for line in lines] == ["press 0", "press 1", "press 2"]


def test_write_unknown_device_serializes_none_model_and_room(tmp_path: Path) -> None:
    writer = CaptureWriter(tmp_path)
    burst = Burst(messages=(MSG_A,), opened_at=0.0, closed_at=0.5)
    now = datetime.now()

    writer.write(
        burst,
        started_at=now,
        closed_at=now,
        device={"id": "0x99", "model": None, "room": None},
        button="unknown button",
        note="",
    )

    record = json.loads(writer.path.read_text())
    assert record["device"] == {"id": "0x99", "model": None, "room": None}


def test_write_serializes_unset_read_at_as_null(tmp_path: Path) -> None:
    """A Message that never passed through SerialReader (e.g. constructed
    directly, as protocol.py always hands them back) has read_at=None -
    confirm that serializes as JSON null rather than raising or coercing
    to something misleading like 0."""
    writer = CaptureWriter(tmp_path)
    unstamped = Message(cycle=1, text="AA", dev_id=0x05, to_master=True)
    burst = Burst(messages=(unstamped,), opened_at=0.0, closed_at=0.5)

    writer.write(
        burst,
        started_at=datetime.now(),
        closed_at=datetime.now(),
        device={"id": "0x05", "model": None, "room": None},
        button="",
        note="",
    )

    record = json.loads(writer.path.read_text())
    assert record["frames"][0]["t"] is None
