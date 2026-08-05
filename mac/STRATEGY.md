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
  requirements.txt
  README.md
  cresnetmon/
    __init__.py
    protocol.py      # pure state machine, no I/O
    serial_io.py      # port enumeration + background reader thread
    ui.py              # tkinter/ttk app
    config.py          # window geometry / last-used settings persistence
    main.py            # entry point
  tests/
    test_protocol.py
```

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

Tasks are sequential (2 depends on 1; 3-6 depend on 2; 7-8 depend on the
rest) but 2 is independently testable/valuable without hardware or a GUI —
started first after scaffolding.
