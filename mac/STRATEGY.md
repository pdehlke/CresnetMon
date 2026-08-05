# CresnetMon macOS Port — Strategy

## Decision
Rewrite (not port) as Python 3 + `pyserial` + `tkinter`/`ttk`. Stdlib GUI, one
external dependency. Chosen over .NET8+Avalonia and native Swift for fastest
path to a working prototype; native `.app` feel is a later concern
(packaging task), not a blocker for a working tool.

## Source of truth
Original app: `CresnetMon/MainForm.cs` (WinForms, .NET Framework 3.5).
Protocol state machine: `MainForm.cs:141-223` (`CresNetProcessByte`,
`ShowMessage`). Constants: `MainForm.cs:17-22`. This logic is being
translated line-for-line, not redesigned — it works, no reason to touch it.

## Target layout
```
mac/
  STRATEGY.md
  pyproject.toml   # uv + hatchling, deps: pyserial (dev: ruff, pytest)
  README.md
  src/cresnetmon/
    __init__.py
    protocol.py      # pure state machine, no I/O
    serial_io.py      # port enumeration + background reader thread
    ui.py              # tkinter/ttk app
    config.py          # window geometry / last-used settings persistence
    main.py            # entry point
  tests/
    test_protocol.py
```
Package management is `uv` (`uv sync`, `uv run ...`), per this user's standing
Python conventions — not `pip`/`venv`.

## Protocol notes (for task 2)
- States: `Searching → Ready → Addressed → Payload` (`MainForm.cs:29-35`)
- Constants: `MasterAddr=0x02`, `MinMsgAddr=0x02`, `MaxMsgAddr=0xFE`,
  `MaxMsgSize=30` (`MainForm.cs:19-22`)
- Byte 0x00 in `Searching` → `Ready`. In `Ready`, a byte in
  `[MinMsgAddr, MaxMsgAddr)` → `Addressed` (records dest id, and send id if
  not master addr); 0x00 stays in `Ready`; anything else → back to
  `Searching`.
- In `Addressed`: byte > `MaxMsgSize` → `Searching`; nonzero byte → message
  size, move to `Payload`; zero byte → back to `Ready` and, if dest != master,
  counts a polling cycle (first distinct dest id seen becomes the poll
  reference id; a display "tick" fires only when dest matches it).
- In `Payload`: accumulate bytes until size reaches zero, then format as
  space-separated hex pairs, emit `(text, dev_id, to_master)` where dev_id is
  send id if dest was master else dest id, `to_master = (dest == master)`.
  Device-id filter (0 = all) is applied at emit time, same as
  `ShowMessage` (`MainForm.cs:211-223`).

## macOS serial specifics
- No registry-style `SerialPort.GetPortNames()` equivalent; enumerate via
  `serial.tools.list_ports.comports()` (pyserial, cross-platform) — covers
  the USB-RS485 adapter's `/dev/cu.usbserial-*` / `/dev/tty.usbserial-*`
  device node.
- Baud fixed at 38400, matching original (`MainForm.cs:90`).
- Background reader thread reads bytes and calls into `protocol.py`;
  messages handed to UI thread via `queue.Queue` + `tk.after()` polling
  (tkinter is not thread-safe, no `BeginInvoke` equivalent).

## Labeling / capture mode
CresnetMon's original scope was read-only display. This adds a second
capability: build a labeled ground-truth dataset correlating known physical
actions (keypad button presses) with the raw bus frames they produce, for
consumption by a not-yet-built Home Assistant automation constructor in the
`homeassistant` repo (`crestron-migration.md`, `crestron-strategy.md` there).

**Why this exists.** Per `crestron-strategy.md`'s Path B, the actual blocker
to bypassing Crestron's MC2E for lighting is that the CLX command frame
format is undocumented — "what bytes actually tell a CLX-1DIM8 to set a
channel to a given level" is unknown. Pressing a keypad button with a known,
observable effect and capturing what crosses the bus as a result is the way
to get ground truth for that decode, without any Crestron software. The same
capture also doubles as a keypad-identity map (which physical button is
which Cresnet frame), useful both for an interim keypad→HA-trigger bridge and
for scoping the eventual keypad replacement. One capture serves both.

**Trigger model — arm, then a silence-bounded burst window.** The bus never
goes quiet; `PollTick` events fire continuously as routine polling and are
not signal. Clicking **Arm** (enabled only while monitoring is running) does
nothing until the *next* `Message` event (never a `PollTick`) appears — that
opens the burst window. The window stays open, capturing every subsequent
`Message`, and closes after a fixed quiet gap (no new `Message`, e.g. 500ms)
with no bus activity. One physical button press typically produces more than
one frame (e.g. the keypad's own report to the master, and the downstream
command the master sends to the target CLX module) — the burst window is
what keeps those correlated as a single event instead of arriving as
disconnected rows.

**Capture scope is always unfiltered**, independent of whatever device-ID
filter is set for the plain live view. A keypad press and the module command
it triggers are two different device IDs; filtering to one during labeling
would silently drop the other half of every event.

**On burst close, a label dialog prompts for:**
- Device — a dropdown seeded from the known device map (defaults to
  whichever device ID appeared first in the burst)
- Button/action — free text (e.g. "button 3" or "dim up"); no button-number
  vocabulary is assumed since that's part of what's being discovered
- Note (optional) — the expected effect, e.g. "Great Room cans to 100%",
  useful for correlating against the CLX frame's meaning later

Submitting **auto-rearms** immediately — one Arm click covers an entire
labeling walk through a room or the house; a separate Disarm/Stop ends it.
Known limitation: the dialog blocks re-arming while it's open, so a button
pressed while still labeling the previous one is missed. Acceptable for a
deliberate, one-press-at-a-time reverse-engineering session; revisit if it's
actually a problem in practice.

**Seed device map.** `crestron-migration.md` already has the full
`REPORTCRESNET` device table (Cresnet ID → model → room). Vendor a static
copy at `mac/seed/devices.json`, hand-copied from those tables — self
-contained (no assumption the `homeassistant` repo is even checked out on
this machine), at the cost of manual re-sync if the house's device map ever
changes. It's already flagged "settled, don't re-check" over there, so drift
risk is low. This seed also upgrades the plain live view to show human
labels ("0x67 Foyer keypad") instead of raw hex.

**Output: JSON Lines, one labeled event per line**, written to
`mac/captures/<session-start-timestamp>.jsonl` (gitignored — it's session
data from a real house, not source). Append-only, flushed after every label,
so a bad session doesn't corrupt earlier reps. Record shape:

```json
{
  "burst_started": "2026-08-05T15:41:12.114-05:00",
  "burst_closed": "2026-08-05T15:41:12.640-05:00",
  "frames": [
    {"dev_id": "0x67", "cycle": 118, "text": "11 22 33", "to_master": true},
    {"dev_id": "0x70", "cycle": 118, "text": "AA 01 64", "to_master": false}
  ],
  "device": {"id": "0x67", "model": "CNX-B8", "room": "103 - Foyer"},
  "button": "button 3 (dim up)",
  "note": "Foyer cans should ramp to 100%"
}
```

`frames` is the raw `Message` events from `protocol.py`, unmodified — no new
parsing logic, just grouping by burst.

No cross-repo path is hardcoded. When a session is ready to feed the HA
automation constructor, the `.jsonl` file is copied into the `homeassistant`
repo by hand.

## Task breakdown (~1 hour each)
1. **Scaffold** — dir/package structure above, `requirements.txt`
   (`pyserial`), stub `main.py` that opens an empty window, mac README.
2. **Protocol module + tests** — `protocol.py` pure translation of
   `CresNetProcessByte`/`ShowMessage`; `tests/test_protocol.py` feeding
   synthetic byte sequences, asserting emitted messages/ids match expected.
3. **Serial layer** — port listing, open/close, background reader thread
   feeding `protocol.py`, thread-safe message queue.
4. **UI shell** — window, port dropdown + refresh, device-id entry,
   start/stop/clear buttons, `ttk.Treeview` (Cycle, Time, Dev, Sent,
   Received columns), status bar (polling count).
5. **Wire UI to serial+protocol** — start/stop handlers, queue polling into
   Treeview rows, hex device-id parsing/validation with error dialog on bad
   input or port-open failure.
6. **Persistence** — window geometry + last port/device-id saved to
   `~/Library/Application Support/CresnetMon/config.json`, restored on
   launch (replaces `FormSettings.cs`).
7. **Packaging** — `pyinstaller` spec (or `py2app`) to produce a `.app`
   bundle; icon; smoke-test launch from Finder.
8. **End-to-end test + polish** — script that feeds a known byte sequence
   into the serial layer (loopback or mock port) and diffs displayed rows
   against expected output; update mac README with usage instructions.
9. **Burst/capture grouping** — pure logic layer above `protocol.py`
   (no I/O): groups `Message` events into bursts per the silence-window
   rule above, ignoring `PollTick`. Own tests, synthetic event sequences.
10. **Seed device map** — `mac/seed/devices.json` hand-copied from
    `crestron-migration.md`'s `REPORTCRESNET` tables, plus a loader; wire
    into the plain live view so rows show human labels instead of raw hex.
11. **Labeling UI** — Arm/Disarm button (enabled only while running), label
    dialog (device dropdown, button/action text, optional note), auto-rearm
    on submit.
12. **JSONL capture writer** — `mac/captures/<timestamp>.jsonl`, append one
    record per labeled burst, flushed immediately; wire to the labeling UI.

Tasks are sequential (2 depends on 1; 3-6 depend on 2; 7-8 depend on the
rest) but 2 is independently testable/valuable without hardware or a GUI —
started first after scaffolding. 9 depends only on 2 (pure logic, buildable
independently of the UI); 10-12 depend on 5 (need a working live UI to hang
Arm/labeling off of) and on 9.
