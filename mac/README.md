# CresnetMon (macOS)

Python port of the Windows CresnetMon tool. Monitors a Crestron Cresnet
RS-485 bus via a USB-RS485 adapter (e.g. SparkFun BOB-09822) and displays
messages sent between devices. See `../README.md` for background on the
Cresnet protocol and hardware, and `STRATEGY.md` for the port plan.

A labeling/capture mode is designed but not yet built (tasks 9-12): arm it,
press a physical keypad button, and it prompts for what that button does,
then writes a labeled JSON-Lines record correlating the button with the bus
frames it produced. Ground-truth data for reverse-engineering the Cresnet
command format and for a Home Assistant automation builder — see
`STRATEGY.md`'s "Labeling / capture mode" section for the full design and
why.

## Setup

```
cd mac
uv sync
```

## Run

```
uv run python -m cresnetmon.main
```

## Usage

Select the serial port from the dropdown (Refresh re-scans if you plug in
the USB-RS485 adapter after launch), and optionally enter a device ID in
hex to monitor just that device - leave it blank or enter 0 to monitor
everything on the bus. Changing either field has no effect once monitoring
is already running.

Click **Start** to begin monitoring; the button toggles to **Stop**. On an
active bus the status bar's polling-cycle count climbs quickly.

The table columns:

| Column | Meaning |
| :--- | :--- |
| ID | Polling cycle count at the time of the message |
| Time | Wall-clock time the message was parsed |
| Dev | The device ID the message was sent to or received from (hex) |
| Sent / Received | The message payload as hex bytes, relative to the bus controller (master) |

**Clear** resets the table and the polling-cycle counter. Window position/
size, the last-used port, and the last-used device ID are remembered
between launches.

## Build a standalone .app

```
uv sync --group packaging
uv run --group packaging pyinstaller build_app.spec --noconfirm
open dist/CresnetMon.app
```

Produces `dist/CresnetMon.app` (~29MB, onedir build), double-click-launchable
from Finder, no Python install required on the target Mac. No custom icon
yet (uses the PyInstaller/macOS default) - deferred, see `STRATEGY.md`.

## Development

```
uv run pytest       # unit + end-to-end tests (fake serial ports, no hardware needed)
uv run ruff check .
uv run ruff format .
```

Status: work in progress — see `STRATEGY.md` for the task breakdown and
current state.
