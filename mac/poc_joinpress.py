"""Press an XPanel button join and watch whether the house responds.

This is the first thing in the project that writes. Everything before it either
listened, or injected onto Cresnet where the modules ignored us.

Why this is expected to work where Cresnet injection did not: we are not
fighting the master for the wire. We register on IP-ID 03, an XPanel slot the
program already contains and nothing else occupies, and press a button. The
processor then drives the dimmers itself, in their own slots, at its own pace.

Why it is safe to try a join whose meaning is not yet certain: the compiled
program retrieved from the processor contains no alarm, security, or access
control of any kind. `G-Security` in it is a *lighting scene*, letter G in a set
of eight running A-Welcome through H-Entertain. The worst outcome of pressing
the wrong join here is that some lights change.

Join 24 is the first candidate because the processor reported digital joins 24
and 35 going high together, and analog join 21 going to 50069, at the moment the
Great Room keypad's Living Pathway button was pressed. 50069 is 0xC395, whose
high byte is exactly the 0xC3 level the Cresnet bus carries for that light.

The press is sent twice, because these buttons toggle. Two presses should leave
the house as it was found.

    uv run python poc_joinpress.py --dry-run
    uv run python poc_joinpress.py --join 24
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys
import time

import crestron_console as ccon
from cip_xpanel import HEARTBEAT, PORT, Listener
from poc_witness import ROLLCALL_FLOOR, Console, Tap, autodetect_port, gate

from cresnetmon.serial_io import PortOpenError, open_port

XPANEL_SLOT, XPANEL_IPID = "E03", 0x03
MODULES = [f"C0x{n:02X}" for n in range(0x70, 0x77)]

SETUP = [
    f"SDEBUG -DON {XPANEL_SLOT}",
    *(f"SDEBUG -DON {m}" for m in MODULES),
    "SDEBUG -RXRON",
    "SDEBUG -TXRON",
    "SDEBUG -SUOFF",
    "SDEBUG -OON",
    "SDEBUG -STON",
]
TEARDOWN = [
    f"SDEBUG -DOFF {XPANEL_SLOT}",
    *(f"SDEBUG -DOFF {m}" for m in MODULES),
    "SDEBUG -RXROFF",
    "SDEBUG -TXROFF",
    "SDEBUG -SUON",
    "SDEBUG -OOFF",
]

HOLD_SECONDS = 0.12  # how long the virtual button stays down


def digital(join: int, pressed: bool) -> bytes:
    """A CIP digital-join packet, mirroring the form the processor sends us.

    The processor renders these as `[03][03][00][17][00]` for "Digital Join 24 is
    High", i.e. datatype 0x00, then the 0-based join low byte, then a byte whose
    top bit is *set* for low and clear for high. Same encoding outbound.
    """
    n = join - 1
    body = bytes([0x00, n & 0xFF, ((n >> 8) & 0x7F) | (0x00 if pressed else 0x80)])
    payload = bytes([0x00, 0x00, len(body)]) + body
    return bytes([0x05, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


def pump(sock: socket.socket, listener: Listener, seconds: float) -> None:
    """Run the CIP receive loop for a while, answering whatever the processor asks."""
    buf = b""
    end = time.monotonic() + seconds
    last_beat = time.monotonic()
    while time.monotonic() < end:
        out: list[bytes] = []
        try:
            data = sock.recv(8192)
            if not data:
                listener.log("connection closed by processor")
                return
            buf += data
        except TimeoutError:
            pass
        while len(buf) >= 3:
            length = (buf[1] << 8) + buf[2]
            if len(buf) < length + 3:
                break
            listener.handle(buf[0], buf[3 : 3 + length], out)
            buf = buf[length + 3 :]
        if not listener.synced and listener.registered and time.monotonic() - listener.last_rx > 2:
            listener.synced = True
            listener.log("state dump quiet, watching for changes")
        if time.monotonic() - last_beat >= 15:
            out.append(HEARTBEAT)
            last_beat = time.monotonic()
        for packet in out:
            sock.sendall(packet)


def main() -> int:
    ap = argparse.ArgumentParser(description="Press an XPanel join and watch the house.")
    ap.add_argument("--join", default="24", help="digital join(s), comma-separated")
    ap.add_argument("--presses", type=int, default=2, help="toggle back by pressing again")
    ap.add_argument("--watch", type=float, default=15.0, help="seconds to watch after each press")
    ap.add_argument("--port", help="serial device (default: autodetect)")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    joins = [int(j) for j in args.join.split(",")]
    downs = [digital(j, True) for j in joins]
    ups = [digital(j, False) for j in joins]
    for j, d, u in zip(joins, downs, ups, strict=True):
        print(f"digital join {j}: press {d.hex(' ')}   release {u.hex(' ')}")
    print(f"{args.presses} press(es), {args.watch:.0f}s watch each, SDEBUG on "
          f"{XPANEL_SLOT} and {len(MODULES)} CLX modules")
    if args.dry_run:
        print("\ndry run, nothing sent")
        return 0

    try:
        port = open_port(args.port or autodetect_port())
    except PortOpenError as exc:
        sys.exit(str(exc))
    console_sock = socket.create_connection((ccon.HOST, ccon.PORT), timeout=8)
    ccon.drain(console_sock, 2.0)

    tap = Tap(port)
    tap.start()
    console = None
    marks: list[tuple[str, float]] = []
    try:
        if gate(tap, 3.0, "tap") < ROLLCALL_FLOOR:
            sys.exit("ABORT: no roll call, the tap is not connected.")
        for cmd in SETUP:
            console_sock.sendall(cmd.encode() + b"\r\n")
            ccon.drain(console_sock, 0.5)
        console = Console(console_sock)
        console.start()

        listener = Listener(XPANEL_IPID, True)
        cip = socket.create_connection((ccon.HOST, PORT), timeout=5)
        cip.settimeout(0.5)
        t0 = time.time()
        marks.append(("connect", t0))
        pump(cip, listener, 8.0)  # register and take the state dump
        if not listener.registered:
            sys.exit("did not register on IP-ID 0x03")

        for i in range(1, args.presses + 1):
            marks.append((f"press{i}", time.time()))
            print(f"\n=== PRESS {i}/{args.presses}: join(s) {args.join} ===", flush=True)
            # Both halves of a two-channel load must go down together, or the
            # program sees two unrelated single-load presses.
            for d in downs:
                cip.sendall(d)
            time.sleep(HOLD_SECONDS)
            for u in ups:
                cip.sendall(u)
            pump(cip, listener, args.watch)

        t_end = time.time()
        cip.close()
        console.stop()

        print("\n=== PROCESSOR (XPanel slot and all CLX modules) ===")
        for ln in console.between(marks[1][1] - 1, t_end):
            print(f"  | {ln}")

        print("\n=== OUR CIP DECODE: changes after sync ===")
        for t, kind, join, value in listener.changes:
            print(f"  {t:7.3f}  {kind}{join} = {value!r}")

        print("\n=== CRESNET BUS: level commands ===")
        buf = tap.between(marks[1][1] - 1, t_end)
        found = 0
        for i in range(len(buf) - 7):
            # Take the size byte from the frame rather than assuming it. Two
            # earlier versions of this check were wrong in turn: one required
            # bytes 3-5 to be 00 00 00 (the keypad's form, missing the C8 the
            # XPanel path uses), and one hard-coded size 0x06, missing every
            # multi-channel frame, which is exactly what a grouped load sends.
            size = buf[i + 1]
            if buf[i + 2] == 0x1D and 6 <= size <= 32 and i + 2 + size <= len(buf):
                print(f"  * {buf[i : i + 2 + size].hex(' ')}")
                found += 1
        print(f"  {found} level command(s)")

        out = pathlib.Path(args.out or f"captures/{time.strftime('%Y%m%dT%H%M%S')}-joinpress.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "joins": joins,
                    "marks": marks,
                    "changes": listener.changes,
                    "console": [[ts, ln] for ts, ln in console.lines],
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
                console_sock.sendall(cmd.encode() + b"\r\n")
                ccon.drain(console_sock, 0.4)
            console_sock.sendall(b"SDEBUG -S1\r\n")
            print("\n=== teardown ===")
            print(ccon.drain(console_sock, 3.0)[:220])
        finally:
            console_sock.close()
            port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
