"""Scan XPanel press joins, recording which dimmer channels each one drives.

The XPanel's press namespace is joins 21-95, named `press21`..`press95` in the
compiled program. The program's name table carries those labels but not the
wiring from a press to a load, so the only way to build that map is to press
each one and watch what the processor does.

That is safe on this processor and only this one: the retrieved program contains
no alarm, security or access control of any kind. `G-Security` in it is a
lighting scene, letter G of eight running A-Welcome through H-Entertain. The
worst case here is that lights change, and every press is followed by a second
press to toggle it back.

Each join is pressed, watched briefly, then pressed again to restore. `SDEBUG`
on all seven CLX modules gives the authoritative record of which module and
channel moved, so the map is built from the processor's own output rather than
from inference.

Two cautions. Some joins are whole-house scenes (House On, House Off, Welcome,
Good Bye), so a scan will briefly light the house. And a join that is a *ramp*
rather than a toggle will not restore itself with a second press.

    uv run python poc_joinscan.py --from 21 --to 95
    uv run python poc_joinscan.py --from 21 --to 95 --until-module 0x70
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import socket
import sys
import time

import crestron_console as ccon
from cip_xpanel import PORT, Listener
from poc_joinpress import MODULES, SETUP, TEARDOWN, digital, pump

FRAME = re.compile(r"CTX:Slot-01\.ID-(7[0-9A-F])\s*:\s*(\[[0-9A-F\]\[]+\])")


def frames_for(lines: list[str]) -> list[str]:
    """Level-setting frames the processor sent, as `ID-71 [71][08][1D]...`."""
    out = []
    for ln in lines:
        m = FRAME.search(ln)
        if m and "[1D]" in m.group(2):
            entry = f"ID-{m.group(1)} {m.group(2)}"
            if entry not in out:  # the console prints each frame twice
                out.append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Map XPanel press joins to dimmer channels.")
    ap.add_argument("--from", dest="lo", type=int, default=21)
    ap.add_argument("--to", dest="hi", type=int, default=95)
    ap.add_argument("--skip", default="", help="comma-separated joins to leave alone")
    ap.add_argument("--watch", type=float, default=2.5, help="seconds held down")
    ap.add_argument("--settle", type=float, default=2.0, help="seconds after releasing")
    ap.add_argument(
        "--until-module",
        help="stop as soon as a join drives this module, e.g. 0x70",
    )
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    skip = {int(s) for s in args.skip.split(",") if s.strip()}
    joins = [j for j in range(args.lo, args.hi + 1) if j not in skip]
    per = args.watch + args.settle
    print(f"scanning joins {args.lo}-{args.hi} ({len(joins)} joins, "
          f"skipping {sorted(skip) or 'none'})")
    print(f"~{per:.1f}s each, about {len(joins) * per / 60:.1f} minutes total")
    if args.until_module:
        print(f"stopping early if a join drives {args.until_module}")
    if args.dry_run:
        print("\ndry run, nothing sent")
        return 0

    console_sock = socket.create_connection((ccon.HOST, ccon.PORT), timeout=8)
    ccon.drain(console_sock, 2.0)
    from poc_witness import Console  # noqa: PLC0415

    console = None
    found: dict[int, list[str]] = {}
    try:
        for cmd in SETUP:
            console_sock.sendall(cmd.encode() + b"\r\n")
            ccon.drain(console_sock, 0.4)
        console = Console(console_sock)
        console.start()
        print(f"SDEBUG armed on {len(MODULES)} modules\n")

        listener = Listener(0x03, False)
        cip = socket.create_connection((ccon.HOST, PORT), timeout=5)
        cip.settimeout(0.4)
        pump(cip, listener, 8.0)
        if not listener.registered:
            sys.exit("did not register on IP-ID 0x03")

        target = args.until_module.lower().replace("0x", "") if args.until_module else None
        for join in joins:
            start = time.time()
            cip.sendall(digital(join, True))
            time.sleep(0.12)
            cip.sendall(digital(join, False))
            pump(cip, listener, args.watch)
            frames = frames_for(console.between(start, time.time()))

            restore = time.time()
            cip.sendall(digital(join, True))
            time.sleep(0.12)
            cip.sendall(digital(join, False))
            pump(cip, listener, args.settle)
            console.between(restore, time.time())  # drain, not analysed

            if frames:
                found[join] = frames
                print(f"  join {join:3d}: {'; '.join(frames)}", flush=True)
                if target and any(f"ID-{target.upper()}" in f for f in frames):
                    print(f"\n*** join {join} drives {args.until_module}, stopping ***")
                    break
            else:
                print(f"  join {join:3d}: -", flush=True)

        cip.close()
        console.stop()

        print(f"\n{len(found)} of {len(joins)} joins drive a dimmer")
        out = pathlib.Path(args.out or f"captures/{time.strftime('%Y%m%dT%H%M%S')}-joinscan.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"found": found, "range": [args.lo, args.hi]}, indent=2))
        print(f"map saved: {out}")
    finally:
        if console is not None:
            console.stop()
        try:
            for cmd in TEARDOWN:
                console_sock.sendall(cmd.encode() + b"\r\n")
                ccon.drain(console_sock, 0.4)
            console_sock.sendall(b"SDEBUG -S1\r\n")
            print("\n=== teardown ===")
            print(ccon.drain(console_sock, 3.0)[:200])
        finally:
            console_sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
