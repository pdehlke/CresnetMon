"""Loads the vendored device-ID seed map (mac/seed/devices.json): human
labels (model + room) for Cresnet IDs on this specific house's bus.

Hand-copied from the homeassistant repo's crestron-migration.md
REPORTCRESNET tables - no live sync with that repo, so this can drift if
the house's device map ever changes; see STRATEGY.md's "Seed device map"
section for the tradeoff. An unlabeled live view (raw hex only) is the
safe fallback if this file is missing, stale, or wrong for a given ID -
never a crash.

Not yet wired into the PyInstaller bundle (task 7's build_app.spec) as a
data file - the path below resolves against the source tree, which won't
exist inside a frozen .app. Fine for now since nothing packaged depends on
it yet; flag this if a future task rebuilds the bundle with labeling mode.
"""

import json
from dataclasses import dataclass
from pathlib import Path

SEED_PATH = Path(__file__).resolve().parent.parent.parent / "seed" / "devices.json"

# Friendly noun per Crestron model, for the live view's human-label
# upgrade. Falls back to the raw model string for anything not listed
# here rather than guessing at unfamiliar hardware.
_MODEL_NOUNS = {
    "CNX-B8": "keypad",
    "CLX-1DIM8": "dimmer",
    "CLX-1DIM4": "dimmer",
    "CLX-4HSW4": "switch",
    "ST-IO": "I/O module",
    "MC2E": "master",
}


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    model: str
    room: str | None = None


def load_seed(path: Path = SEED_PATH) -> dict[int, DeviceInfo]:
    """Read the seed map, keyed by Cresnet device id. Empty dict if the
    file is missing, unreadable, or malformed - see module docstring."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):  # fmt: skip
        return {}
    devices: dict[int, DeviceInfo] = {}
    for entry in raw.get("devices", []):
        try:
            dev_id = int(entry["id"], 16)
        except (KeyError, TypeError, ValueError):  # fmt: skip
            continue
        devices[dev_id] = DeviceInfo(model=entry.get("model", ""), room=entry.get("room"))
    return devices


def format_device_label(dev_id: int, devices: dict[int, DeviceInfo]) -> str:
    """Human label for the live view's Dev column, e.g. "67 Foyer keypad".
    Falls back to plain hex if the id isn't in the seed map."""
    hex_id = f"{dev_id:02X}"
    info = devices.get(dev_id)
    if info is None:
        return hex_id
    noun = _MODEL_NOUNS.get(info.model, info.model)
    label = f"{hex_id} {info.room} {noun}" if info.room else f"{hex_id} {noun}"
    return label.rstrip()
