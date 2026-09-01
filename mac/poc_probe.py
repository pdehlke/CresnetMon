"""Is this adapter actually transmitting onto the Cresnet bus?

Same question as v1, fixed methodology. v1 called `flush()` after every frame,
which on macOS FTDI runs `tcdrain()` and can block for tens of milliseconds. In
a single-threaded read/write loop that starved the reader and overflowed the
adapter's RX buffer, so received bytes collapsed ~90% and the reply counts
measured nothing but our own stalls.

v2 reads on a dedicated thread that never blocks on transmission (the same shape
`serial_io.py` already uses), drops the per-frame flush, and re-measures the
baseline afterwards so a genuinely disturbed bus can be told apart from a
measurement artifact.

Method, unchanged: MC2E polls a device with a bare `<dest> 00` frame and the
device answers `02 00`. Devices can only answer if our bytes physically reached
the wire, so a rise in the reply rate is proof of transmission that does not
depend on the adapter hearing itself. A bare poll changes no state, which makes
this the most benign thing we can put on the bus.

Usage:
    uv run python poc_probe.py
    uv run python poc_probe.py --mode assert --probes 400
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from cresnetmon.serial_io import PortOpenError, list_ports, open_port

PROBE_TARGET = 0x70  # CLX-1DIM8: MC2E polls these ~2.5/s vs ~27/s for keypads
PROBE_FRAME = bytes([PROBE_TARGET, 0x00])
REPLY = b"\x02\x00"
ECHO_CANARY = b"\x50\x00"  # addressed to nothing; 0 natural occurrences in 815KB


class Reader(threading.Thread):
    """Drain the port continuously so transmission can never starve reads."""

    def __init__(self, port) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                chunk = self.port.read(self.port.in_waiting or 1)
            except Exception:  # noqa: BLE001 - port closing under us is expected
                return
            if chunk:
                with self.lock:
                    self.buf += chunk

    def snapshot(self) -> bytes:
        with self.lock:
            return bytes(self.buf)


def count_replies(data: bytes) -> int:
    """Count device-to-master empty frames, walking <dest> <size> <payload>."""
    i = replies = 0
    while i < len(data) - 1:
        dest, size = data[i], data[i + 1]
        if not (0x02 <= dest < 0xFE) or size > 30 or i + 2 + size > len(data):
            i += 1
            continue
        if data[i : i + 2] == REPLY:
            replies += 1
        i += 2 + size
    return replies


def phase(reader: Reader, port, seconds: float, *, probes: int, rts: bool | None) -> dict:
    """Measure one window, optionally transmitting `probes` poll frames into it."""
    start = len(reader.snapshot())
    deadline = time.time() + seconds
    gap = seconds / (probes * 2) if probes else 0
    if rts is not None:
        port.rts = rts
    sent = 0
    while time.time() < deadline:
        if sent < probes:
            port.write(PROBE_FRAME + ECHO_CANARY)  # no flush: that was v1's bug
            sent += 1
            time.sleep(gap)
        else:
            time.sleep(0.01)
    if rts is not None:
        port.rts = False
    data = reader.snapshot()[start:]
    return {
        "bytes": len(data),
        "replies": count_replies(data),
        "canary": data.count(ECHO_CANARY),
        "sent": sent,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", help="serial device (default: autodetect)")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--probes", type=int, default=250)
    parser.add_argument("--mode", choices=("none", "assert", "deassert"))
    args = parser.parse_args()

    device = args.port
    if not device:
        found = [p for p in list_ports() if "usbserial" in p.device or "usbmodem" in p.device]
        if len(found) != 1:
            sys.exit("pass --port explicitly; found: " + ", ".join(p.device for p in found))
        device = found[0].device

    modes: dict[str, bool | None] = {"none": None, "assert": True, "deassert": False}
    if args.mode:
        modes = {args.mode: modes[args.mode]}

    try:
        port = open_port(device)
    except PortOpenError as exc:
        sys.exit(str(exc))

    reader = Reader(port)
    reader.start()
    print(f"port {device} @38400 8N1, reader thread running")
    print(f"probe {PROBE_FRAME.hex(' ')} + canary {ECHO_CANARY.hex(' ')}\n")

    hdr = f"{'phase':12} {'bytes':>7} {'replies':>8} {'/s':>7} {'sent':>6} {'canary':>7}"
    try:
        rows = []
        base = phase(reader, port, args.seconds, probes=0, rts=None)
        rows.append(("baseline", base))
        for name, rts in modes.items():
            res = phase(reader, port, args.seconds, probes=args.probes, rts=rts)
            rows.append((f"tx {name}", res))
        rows.append(("baseline#2", phase(reader, port, args.seconds, probes=0, rts=None)))

        print(hdr)
        base_rate = base["replies"] / args.seconds
        for name, r in rows:
            rate = r["replies"] / args.seconds
            print(
                f"{name:12} {r['bytes']:7} {r['replies']:8} {rate:7.0f} "
                f"{r['sent']:6} {r['canary']:7}"
            )
        print(f"\nbaseline reply rate: {base_rate:.0f}/s")
    finally:
        reader.stop.set()
        time.sleep(0.1)
        port.rts = False
        port.close()

    print(
        "\nreading the result:\n"
        "  byte counts roughly EQUAL across all phases -> the measurement is sound.\n"
        "    Judge TX by the reply rate.\n"
        "  reply rate up during a tx phase -> we ARE on the wire; devices answered.\n"
        "  reply rate flat in every mode, bytes steady -> nothing reaches the wire.\n"
        "  baseline#2 well below baseline -> we disturbed the bus; stop and re-check\n"
        "    before injecting anything else.\n"
        "  canary > 0 -> the adapter echoes its own TX."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
