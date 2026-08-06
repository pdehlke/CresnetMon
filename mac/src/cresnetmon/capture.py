"""Writes labeled capture events to mac/captures/<session-start>.jsonl -
one JSON object per line, opened/appended/closed fresh on every write so
each record is flushed immediately and a crash mid-session can't corrupt
earlier reps. Input for the not-yet-built Home Assistant automation
constructor in the homeassistant repo - see STRATEGY.md's "Labeling /
capture mode" section for the record shape and why.

Decoupled from devices.py/protocol.py beyond the Burst type itself: the
caller (app.py) assembles the `device` dict and the wall-clock bounds;
this module only serializes what it's given.
"""

import json
from datetime import datetime
from pathlib import Path

from cresnetmon.burst import Burst

CAPTURES_DIR = Path(__file__).resolve().parent.parent.parent / "captures"


class CaptureWriter:
    """One instance per labeling session: all labeled bursts from one app
    launch go to the same file, named for when the writer was created."""

    def __init__(self, directory: Path = CAPTURES_DIR) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        session_name = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.path = directory / f"{session_name}.jsonl"

    def write(
        self,
        burst: Burst,
        *,
        started_at: datetime,
        closed_at: datetime,
        device: dict[str, str | None],
        button: str,
        note: str,
    ) -> None:
        """Append one labeled-burst record and flush it to disk."""
        record = {
            "burst_started": started_at.isoformat(),
            "burst_closed": closed_at.isoformat(),
            "frames": [
                {
                    "dev_id": f"0x{message.dev_id:02X}",
                    "cycle": message.cycle,
                    "text": message.text,
                    "to_master": message.to_master,
                }
                for message in burst.messages
            ],
            "device": device,
            "button": button,
            "note": note,
        }
        with self.path.open("a") as handle:
            handle.write(json.dumps(record))
            handle.write("\n")
