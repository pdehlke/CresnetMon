"""Tests for cresnetmon.rawlog.RawLogWriter. Uses tmp_path so nothing
touches the real mac/captures/ directory."""

import json
from datetime import datetime
from pathlib import Path

from cresnetmon.rawlog import RawLogWriter


def test_writer_creates_captures_directory(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    assert not directory.exists()

    RawLogWriter(directory)

    assert directory.is_dir()


def test_writer_names_file_after_session_start_with_raw_suffix(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)

    assert writer.path.parent == tmp_path
    assert writer.path.name.endswith("-raw.jsonl")
    # e.g. 20260831T075334-raw.jsonl - just check the stem parses as a timestamp.
    session_name = writer.path.name.removesuffix("-raw.jsonl")
    datetime.strptime(session_name, "%Y%m%dT%H%M%S")


def test_write_appends_one_json_line_with_expected_shape(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)

    writer.write(bytes([0x00, 0x62, 0x03, 0x11, 0x22, 0x33]), t=1756642872.114)

    lines = writer.path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {"t": 1756642872.114, "hex": "00 62 03 11 22 33"}


def test_write_appends_multiple_records_on_separate_lines(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)

    writer.write(bytes([0x00]), t=1.0)
    writer.write(bytes([0x05, 0x00]), t=1.05)
    writer.write(bytes([0x07]), t=1.1)

    lines = writer.path.read_text().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [r["hex"] for r in records] == ["00", "05 00", "07"]
    assert [r["t"] for r in records] == [1.0, 1.05, 1.1]


def test_concatenated_hex_reproduces_exact_byte_stream(tmp_path: Path) -> None:
    """The core contract: concatenating every record's hex field, in file
    order, must reproduce the exact input byte stream - that's what makes
    an offline re-parse of a session possible (STRATEGY.md task 14)."""
    writer = RawLogWriter(tmp_path)
    chunks = [bytes([0x00, 0x05]), bytes([0x00, 0x02, 0x11, 0x22]), bytes([0x00])]

    for i, chunk in enumerate(chunks):
        writer.write(chunk, t=float(i))

    lines = writer.path.read_text().splitlines()
    reassembled = bytearray()
    for line in lines:
        record = json.loads(line)
        reassembled.extend(bytes.fromhex(record["hex"].replace(" ", "")))

    assert bytes(reassembled) == b"".join(chunks)


def test_write_handles_empty_chunk(tmp_path: Path) -> None:
    """Not expected in practice (app.py only calls write() with a non-empty
    chunk), but write() itself shouldn't choke on one."""
    writer = RawLogWriter(tmp_path)

    writer.write(b"", t=0.0)

    record = json.loads(writer.path.read_text())
    assert record == {"t": 0.0, "hex": ""}
