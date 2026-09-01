"""Writes the raw, undecoded serial byte stream to
mac/captures/<session-start>-raw.jsonl - one JSON object per chunk of bytes
drained off the serial reader's raw queue, opened/appended/closed fresh on
every write, same durability posture as capture.CaptureWriter.

This exists to close the gap flagged in STRATEGY.md task 14: protocol.py
and burst.py both deliberately discard routine polling traffic (PollTick
carries no payload; BurstGrouper ignores it entirely), so the master's
polling behavior - the thing an eventual bridge most needs to replicate
correctly - is invisible in the labeled JSONL capture. This log keeps
every byte, decoded or not, so a session can be re-parsed offline later.

Off by default (STRATEGY.md's Raw log toggle in ui.py) since a long
session at 38400 baud produces tens of MB of hex text - fine for a
deliberate reverse-engineering session, wasteful as an always-on default.

Session-scoped to one Start/Stop cycle, not one app launch like
CaptureWriter: raw logging is meant to start and stop with monitoring
(STRATEGY.md task 14), since poll traffic - the whole point of this log -
is exactly what happens while nothing is armed. app.py creates a fresh
writer on every Start rather than reusing one across a launch's multiple
Start/Stop cycles.
"""

import json
from datetime import datetime
from pathlib import Path

CAPTURES_DIR = Path(__file__).resolve().parent.parent.parent / "captures"


class RawLogWriter:
    """One instance per monitoring run (Start to Stop): every raw chunk
    drained during that run goes to the same file, named for when the
    writer was created."""

    def __init__(self, directory: Path = CAPTURES_DIR) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        session_name = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.path = directory / f"{session_name}-raw.jsonl"

    def write(self, data: bytes, *, t: float) -> None:
        """Append one raw chunk and flush it to disk. `t` is the chunk's
        timestamp (float epoch seconds) - one per chunk, not per byte, per
        STRATEGY.md task 14. Concatenating every record's `hex` field, in
        file order, reproduces the exact byte stream `data` came from."""
        record = {"t": t, "hex": " ".join(f"{b:02X}" for b in data)}
        with self.path.open("a") as handle:
            handle.write(json.dumps(record))
            handle.write("\n")
