# CresnetMon (macOS)

Python port of the Windows CresnetMon tool. Monitors a Crestron Cresnet
RS-485 bus via a USB-RS485 adapter (e.g. SparkFun BOB-09822) and displays
messages sent between devices. See `../README.md` for background on the
Cresnet protocol and hardware, and `STRATEGY.md` for the port plan.

## Setup

```
cd mac
uv sync
```

## Run

```
uv run python -m cresnetmon.main
```

Status: work in progress — see `STRATEGY.md` for the task breakdown and
current state.
