# CresnetMon (macOS)

## What this is

Crestron home automation systems (lighting keypads, dimmer modules, touch
panels, and so on) talk to each other over a wired bus called Cresnet.
CresnetMon is a passive listener for that bus: it doesn't control
anything, it just shows you the raw traffic going by, byte by byte,
translated into a readable table. That's useful for troubleshooting an
existing Crestron install, or for reverse-engineering what a system is
doing when you don't have (or don't want to depend on) Crestron's own
programming software.

This is a Python rewrite of the original Windows Forms tool in this repo
(`../CresnetMon`), built to run natively on macOS. See `../README.md` for
background on the original project and a screenshot of the Windows
version's UI (this one looks similar, just native to macOS).

## Hardware you need

A USB-to-RS485 adapter, physically wired to the Cresnet bus. Cresnet runs
over two signal wires, commonly labeled **Y** and **Z** (equivalent to a
standard RS-485 A/B pair), plus a ground reference - these are usually
accessible at a keypad's wall-box wiring, a lighting module's terminal
block, or a Crestron control processor's own Cresnet port. Any adapter
using a common serial chipset (FTDI, Silicon Labs CP210x, CH340, etc.)
should work; the [SparkFun USB-RS485 Converter](https://www.sparkfun.com/products/9822)
(BOB-09822) is what the original Windows app's README recommends, but this
port has also been tested against a plain FTDI FT232R adapter with no
special driver installation on a recent macOS.

**Do not connect the adapter's own power/data-common lines to Cresnet's
24V power wiring** - Cresnet carries its own 24VDC on separate terminals
from the Y/Z data pair, and a USB adapter should only ever touch the two
data wires (plus ground/common if the adapter needs it), never the power
rail. If you're not confident identifying the right terminals on your own
system, get help from whoever installed it before wiring anything up.

Once wired in and plugged into the Mac, the adapter should show up as a
serial device - check with:

```
ls /dev/cu.*
```

You're looking for something like `/dev/cu.usbserial-XXXXXXXX`. If nothing
shows up, the adapter may need a driver from its manufacturer (search for
the chipset name, e.g. "CP2102 macOS driver").

## Prerequisites

- macOS
- [uv](https://docs.astral.sh/uv/getting-started/installation/) - handles
  the Python install and virtual environment for you, nothing else to set
  up first:
  ```
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

## Install

```
cd mac
uv sync
```

## Run

```
uv run python -m cresnetmon.main
```

## Using the monitor

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
| Dev | The device ID the message was sent to or received from (hex, or a human label - see "Device labels" below) |
| Sent / Received | The message payload as hex bytes, relative to the bus controller (master) |

**Clear** resets the table and the polling-cycle counter. Window position/
size, the last-used port, and the last-used device ID are remembered
between launches.

## Labeling / capture mode (optional, advanced)

This is for reverse-engineering, not everyday monitoring - skip this
section if you just want to watch bus traffic.

While monitoring is running, click **Arm**. It does nothing until the next
real message crosses the bus (routine polling traffic is ignored); once
one does, it keeps collecting for a short quiet period after the last
message, then pops up a dialog asking what you just did - physically press
a specific keypad button (or trigger whatever action you're trying to
identify) right after clicking Arm, so the frame(s) it captures are
actually caused by that action, not something else on a busy bus.

The dialog asks for:

- **Device** - a dropdown of known devices (see "Device labels" below),
  defaulting to whichever device showed up first in the captured frames
- **Button/action** - free text describing what you did (e.g. "button 3,
  dim up")
- **Note** (optional) - the effect you expected (e.g. "Great Room cans to
  100%")

Submitting writes one JSON record to `mac/captures/<session-start-time>.jsonl`
(one file per app launch, one line per labeled event, created on first use)
and immediately re-arms for the next press - arm once, then walk around
pressing buttons in sequence. Click **Disarm** to stop. Cancel discards
that one capture without writing it, but still re-arms.

This data is meant as ground truth for correlating physical actions with
the raw bytes they produce on the bus - useful input if you're trying to
decode what a specific button or command actually does at the protocol
level.

## Device labels (seed data)

`mac/seed/devices.json` is a small file mapping Cresnet device IDs to a
model name and room, so the monitor can show something like `67 Foyer
keypad` instead of just `67`. It's entirely optional cosmetic sugar - the
monitor works fine with no seed data at all, it just shows raw hex IDs for
everything.

**The file shipped in this repo describes the original author's own
house.** If you're using this on a different Crestron system, replace it
with your own before relying on the labels - otherwise you'll see rooms
and device names that mean nothing on your system.

To build your own seed file, the most direct route is Telnet access to
your Crestron control processor(s), if you have it (no SIMPL, VTPro-e, or
Crestron Toolbox license needed):

```
telnet <processor-ip> 23
```

Once connected, the `REPORTCRESNET` console command lists every device on
that processor's Cresnet bus with its ID and model, e.g.:

```
62: CNX-B8
70: CLX-1DIM8
```

Room names aren't always in that output - if your processor's loaded
program is available, `TYPE <program-name>.dsc` at the console often
includes a per-device room label you can cross-reference. Exact commands
and output format can vary by processor model and firmware, so treat this
as a starting point, not a guarantee.

If you don't have console access, or don't want to bother, just leave the
seed file as an empty device list - the monitor still works, IDs just show
as plain hex:

```json
{
  "source": "<a note to yourself about where this came from>",
  "devices": []
}
```

Or fill in whatever devices you do know about, in the same shape:

```json
{
  "source": "my house, telnet REPORTCRESNET, 2026-01-01",
  "devices": [
    {"id": "0x62", "model": "CNX-B8", "room": "Living Room"},
    {"id": "0x70", "model": "CLX-1DIM8", "room": "Hallway"}
  ]
}
```

`id` is the Cresnet device ID as a hex string (`0x` prefix, two digits).
`model` and `room` are free text; `room` can be omitted or `null` if
unknown. No code changes needed - just edit the JSON file and restart the
app.

## Build a standalone .app

```
uv sync --group packaging
uv run --group packaging pyinstaller build_app.spec --noconfirm
open dist/CresnetMon.app
```

Produces `dist/CresnetMon.app` (~29MB, onedir build), double-click-launchable
from Finder, no Python install required on the target Mac. No custom icon
yet (uses the PyInstaller/macOS default). The seed device file (above)
isn't bundled into the `.app` yet either - run from source (`uv run
python -m cresnetmon.main`) if you need labeling mode with device labels.

## Development

```
uv run pytest       # unit + end-to-end tests (fake serial ports, no hardware needed)
uv run ruff check .
uv run ruff format .
```

See `STRATEGY.md` for the full design, task history, and what's left.
