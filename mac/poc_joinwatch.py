"""Watch three passive streams at once while someone presses a physical keypad.

The point is to identify which XPanel join corresponds to a known light, without
pressing any join ourselves. Everything here is read-only: SDEBUG print flags
with guaranteed teardown, a CIP registration that sends nothing but the
handshake and heartbeats, and a serial tap that only reads.

Three views, deliberately redundant:

1. **SDEBUG on the XPanel slot.** The processor's own interpretation, printed as
   "Digital Join N is High". Authoritative, and immune to bugs in our decoder.
   This is the primary evidence.
2. **`cip_xpanel.py` registered on IP-ID 03.** Our decode of the same traffic.
   Kept as a cross-check precisely because it has been wrong before: the three
   multi-join decoding bugs fixed after the first IP-ID-03 test are the likely
   reason that test wrongly concluded the slot was silent.
3. **The Cresnet tap.** Independently confirms the button was physically pressed
   and shows which dimmer channels actually moved, so a join can be tied to a
   load rather than merely to a moment in time.

    uv run python poc_joinwatch.py --seconds 300
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import sys
import time

import crestron_console as ccon
from poc_witness import ROLLCALL_FLOOR, Console, Tap, autodetect_port, gate

from cresnetmon.serial_io import PortOpenError, open_port

XPANEL_SLOT = "E03"
XPANEL_IPID = "0x03"

SETUP = [
    f"SDEBUG -DON {XPANEL_SLOT}",
    "SDEBUG -RXRON",
    "SDEBUG -TXRON",
    "SDEBUG -RXION",
    "SDEBUG -TXION",
    "SDEBUG -SUOFF",
    "SDEBUG -OON",
    "SDEBUG -STON",
]

# Restores what the processor reported before this run.
TEARDOWN = [
    f"SDEBUG -DOFF {XPANEL_SLOT}",
    "SDEBUG -RXROFF",
    "SDEBUG -TXROFF",
    "SDEBUG -SUON",
    "SDEBUG -OOFF",
]


def notable(buf: bytes) -> list[str]:
    """Cresnet frames worth correlating: keypad traffic and dimmer level sets."""
    out: list[str] = []
    i = 0
    while i < len(buf) - 1:
        dest, size = buf[i], buf[i + 1]
        if i + 2 + size > len(buf):
            break
        payload = buf[i + 2 : i + 2 + size]
        if size and (dest in range(0x62, 0x70) or payload[0] == 0x1D):
            out.append(buf[i : i + 2 + size].hex(" "))
        i += 2 + size if size else 2
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Correlate XPanel joins with a physical keypress.")
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--port", help="serial device (default: autodetect)")
    ap.add_argument("--out")
    args = ap.parse_args()

    try:
        port = open_port(args.port or autodetect_port())
    except PortOpenError as exc:
        sys.exit(str(exc))

    sock = socket.create_connection((ccon.HOST, ccon.PORT), timeout=8)
    ccon.drain(sock, 2.0)

    tap = Tap(port)
    tap.start()
    console = None
    try:
        if gate(tap, 3.0, "tap") < ROLLCALL_FLOOR:
            sys.exit("ABORT: no roll call, the tap is not connected.")

        for cmd in SETUP:
            sock.sendall(cmd.encode() + b"\r\n")
            ccon.drain(sock, 0.8)
        console = Console(sock)
        console.start()

        t0 = time.time()
        print(
            f"LISTENING {args.seconds:.0f}s: SDEBUG on {XPANEL_SLOT}, "
            f"CIP on IP-ID {XPANEL_IPID}, and the Cresnet tap. Press the keypad button now.",
            flush=True,
        )

        cip = subprocess.run(
            ["uv", "run", "python", "cip_xpanel.py",
             "--seconds", str(int(args.seconds)), "--ipid", XPANEL_IPID, "-v"],
            cwd=str(pathlib.Path(__file__).parent),
            capture_output=True, text=True, timeout=args.seconds + 90,
        )
        t_end = time.time()
        console.stop()

        print("\n=== 1. PROCESSOR (SDEBUG on the XPanel slot) ===")
        lines = console.between(t0, t_end)
        print(f"{len(lines)} lines")
        for ln in lines:
            print(f"  | {ln}")

        print("\n=== 2. OUR CIP DECODE (IP-ID 03) ===")
        print(cip.stdout[-6000:])
        if cip.stderr.strip():
            print("STDERR:", cip.stderr[-1000:])

        print("\n=== 3. CRESNET BUS (keypad frames and level sets) ===")
        frames = notable(tap.between(t0, t_end))
        print(f"{len(frames)} notable frames")
        for f in frames:
            print(f"  * {f}")

        out = pathlib.Path(args.out or f"captures/{time.strftime('%Y%m%dT%H%M%S')}-joinwatch.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "window": [t0, t_end],
                    "console": [[ts, ln] for ts, ln in console.lines],
                    "cip_stdout": cip.stdout,
                    "bus": [[ts, c.hex(" ")] for ts, c in tap.chunks],
                }
            )
        )
        print(f"\nevidence saved: {out}")
    finally:
        if console is not None:
            console.stop()
        try:
            for cmd in TEARDOWN:
                sock.sendall(cmd.encode() + b"\r\n")
                ccon.drain(sock, 0.6)
            sock.sendall(b"SDEBUG -S1\r\n")
            print("\n=== teardown ===")
            print(ccon.drain(sock, 3.0)[:300])
        finally:
            sock.close()
            port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
