"""Stage 1 of the override test: does the processor undo our lighting command?

`poc_witness.py` established that our writes reach the bus. The MC2E logged our
probe bytes verbatim and the roll call's conformance fell from 100.0% to 80.2%
while we transmitted. So the reason `living_pathway.py` produced no visible
light is not that the bytes went nowhere.

The leading explanation is that the processor maintains dimmer state and
re-asserts it faster than a lamp can respond. A channel driven to 76% and
returned to zero inside one 41ms poll round produces no visible light at all,
which matches the reported "no flicker" better than a timing failure would.

This sends the real Living Pathway command five times over a second, then
watches every CLX module through SDEBUG for a `1D` of the processor's own. A
correction appearing within a round or two confirms the hypothesis. Both on and
off are sent, in that order, so the house is left as it was found.

Nothing here is more invasive than pressing the keypad button, except that the
processor does not know it was pressed.

    uv run python poc_override.py --dry-run
    uv run python poc_override.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys
import time

import crestron_console as ccon
from living_pathway import FRAMES
from poc_witness import (
    CRESNET_TARGETS,
    ROLLCALL_FLOOR,
    SETUP,
    TEARDOWN,
    Console,
    Tap,
    autodetect_port,
    gate,
)

from cresnetmon.serial_io import PortOpenError, open_port

# Five sends was a mistake: sustained injection makes every CLX module on the
# leg re-initialise, which destroys the state we are trying to observe. One send
# perturbs the bus once and then leaves it alone.
SENDS = 1
SEND_GAP = 0.2


def level_frames(buf: bytes) -> list[str]:
    """Every `1D` set-level frame in a raw stream, with its destination."""
    out = []
    for i in range(len(buf) - 7):
        if buf[i + 1] == 0x06 and buf[i + 2] == 0x1D and buf[i + 3 : i + 6] == b"\x00\x00\x00":
            out.append(buf[i : i + 8].hex(" "))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Does the MC2E undo our lighting command?")
    ap.add_argument("--port")
    ap.add_argument("--baseline", type=float, default=8.0)
    ap.add_argument("--watch", type=float, default=12.0, help="seconds to watch after each command")
    ap.add_argument("--sends", type=int, default=SENDS, help="times to send each command")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("Living Pathway, real command, sent behind the processor's back:")
    for action in ("on", "off"):
        for f in FRAMES[action]:
            print(f"  {action:3s}  {f.hex(' ')}")
    print(f"\nplan: {args.baseline:.0f}s quiet, ON x{args.sends}, watch {args.watch:.0f}s, "
          f"OFF x{args.sends}, watch {args.watch:.0f}s")
    print(f"SDEBUG scoped to {' '.join(CRESNET_TARGETS)}")
    if args.dry_run:
        print("\ndry run, nothing touched")
        return 0

    try:
        port = open_port(args.port or autodetect_port())
    except PortOpenError as exc:
        sys.exit(str(exc))

    sock = socket.create_connection((ccon.HOST, ccon.PORT), timeout=8)
    ccon.drain(sock, 2.0)

    tap = Tap(port)
    tap.start()
    console = None
    marks: dict[str, float] = {}
    try:
        print("\n=== liveness gate (before) ===")
        if gate(tap, 3.0, "tap") < ROLLCALL_FLOOR:
            sys.exit("ABORT: no roll call, the tap is not connected. No verdict possible.")

        sock.sendall(b"ERRLOG\r\n")
        before_log = ccon.drain(sock, 5.0)
        print(f"error log before: {before_log.strip().splitlines()[-2:]}")

        for cmd in SETUP:
            sock.sendall(cmd.encode() + b"\r\n")
            ccon.drain(sock, 0.8)
        print(f"SDEBUG armed on {len(CRESNET_TARGETS)} modules")

        console = Console(sock)
        console.start()

        marks["base0"] = time.time()
        print(f"\n=== baseline {args.baseline:.0f}s ===")
        time.sleep(args.baseline)

        for action in ("on", "off"):
            marks[f"{action}0"] = time.time()
            print(f"=== sending {action.upper()} x{args.sends} ===")
            for _ in range(args.sends):
                for frame in FRAMES[action]:
                    port.write(frame)
                    port.flush()
                    time.sleep(0.002)
                time.sleep(SEND_GAP)
            print(f"=== watching {args.watch:.0f}s ===")
            time.sleep(args.watch)
            marks[f"{action}1"] = time.time()

        marks["end"] = time.time()
        print("\n=== liveness gate (after) ===")
        gate(tap, 3.0, "tap")
        console.stop()

        for label, a, b in (
            ("baseline", marks["base0"], marks["on0"]),
            ("ON window", marks["on0"], marks["on1"]),
            ("OFF window", marks["off0"], marks["off1"]),
        ):
            print(f"\n=== {label} ===")
            lines = console.between(a, b)
            print(f"  console: {len(lines)} lines")
            for ln in lines:
                flag = "  <<<" if "[1D]" in ln.upper() else ""
                print(f"    | {ln}{flag}")
            lvl = level_frames(tap.between(a, b))
            print(f"  bus 1D frames seen: {len(lvl)}")
            for f in lvl:
                print(f"    * {f}")

        print("\n=== error log after ===")
        sock.sendall(b"ERRLOG\r\n")
        print(ccon.drain(sock, 6.0))

        out = pathlib.Path(args.out or f"captures/{time.strftime('%Y%m%dT%H%M%S')}-override.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "marks": marks,
                    "sends": args.sends,
                    "frames": {k: [f.hex(" ") for f in v] for k, v in FRAMES.items()},
                    "sdebug_targets": CRESNET_TARGETS,
                    "bus": [[ts, c.hex(" ")] for ts, c in tap.chunks],
                    "console": [[ts, ln] for ts, ln in console.lines],
                }
            )
        )
        print(f"evidence saved: {out}")
    finally:
        if console is not None:
            console.stop()
        try:
            for cmd in TEARDOWN:
                sock.sendall(cmd.encode() + b"\r\n")
                ccon.drain(sock, 0.6)
            sock.sendall(b"SDEBUG -S1\r\n")
            print("\n=== settings after teardown ===")
            print(ccon.drain(sock, 3.0))
        finally:
            sock.close()
            port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
