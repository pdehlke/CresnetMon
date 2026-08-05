# CresnetMon (macOS)

Python port of the Windows CresnetMon tool. Monitors a Crestron Cresnet
RS-485 bus via a USB-RS485 adapter (e.g. SparkFun BOB-09822) and displays
messages sent between devices. See `../README.md` for background on the
Cresnet protocol and hardware, and `STRATEGY.md` for the port plan.

Also supports a labeling/capture mode: arm it, press a physical keypad
button, and it prompts for what that button does, then writes a labeled
JSON-Lines record correlating the button with the bus frames it produced.
Ground-truth data for reverse-engineering the Cresnet command format and for
a Home Assistant automation builder — see `STRATEGY.md`'s "Labeling /
capture mode" section for the full design and why.

## Setup

```
cd mac
uv sync
```

## Run

```
uv run python -m cresnetmon.main
```

## Build a standalone .app

```
uv sync --group packaging
uv run --group packaging pyinstaller build_app.spec --noconfirm
open dist/CresnetMon.app
```

Produces `dist/CresnetMon.app` (~29MB, onedir build), double-click-launchable
from Finder, no Python install required on the target Mac. No custom icon
yet (uses the PyInstaller/macOS default) - deferred, see `STRATEGY.md`.

Status: work in progress — see `STRATEGY.md` for the task breakdown and
current state.
