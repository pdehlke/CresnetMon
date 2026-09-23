"""Measure what a subsystem switch costs on an AADS panel slot, frame by frame.

The `crestron_cip` bridge is going to time-slice one panel slot between the
Lights and A/V subsystems, and its `async_enter()` needs to know when the dump
that follows an entry press is finished. Two signals are candidates and neither
is evidenced yet:

  * the processor's own end-of-query marker (CIP type 0x05, datatype 0x03, body
    byte 0x1C), which would be exact if it fires after a subsystem entry and not
    only after the initial update request, and
  * a quiet window, which needs a threshold larger than every gap between two
    consecutive frames of one dump. Only the first and last arrival times of an
    entry dump have ever been recorded, never the gaps in between.

This script answers both. It registers on a panel slot, presses a sequence of
subsystem-entry joins, and reports for each press: how many frames arrived, the
offset of the first and last, the largest gap between consecutive arrivals, and
whether an end-of-query frame came with it. It finishes with the smallest quiet
threshold that would have worked for every window it saw.

Deliberately standalone and stdlib-only. `poc_panelpress.py` is the obvious tool
for this and cannot run without the Cresnet tap's dependencies despite its own
docstring saying it needs no tap: it imports `poc_joinpress`, which imports
`poc_witness`, which imports `serial`. Nothing here imports anything from this
package.

Design notes this measurement feeds:
  pdehlke/homeassistant, docs/crestron/crestron-subsystem-time-slicing.md
Join map and the round-trip result this refines:
  pdehlke/homeassistant, docs/crestron/crestron-av-zone-control-path.md

    cd mac && uv run python poc_subsystem_timing.py

Run it from `mac/`, which is where this repo's pyproject.toml lives. There is
none at the repo root, so `uv run` from there falls back to whatever interpreter
uv picks by default, which on this machine is 3.9.

No Cresnet tap, no unplugged panel, no SDEBUG.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

# Two things here need 3.10: socket.recv's timeout only became a TimeoutError
# that far back, and ruff formats this file for the project's 3.14 target, which
# includes syntax earlier interpreters cannot parse. Say so rather than let it
# fail somewhere confusing, because the repo root has no pyproject.toml and
# `uv run` from there falls back to an interpreter old enough to hit both.
if sys.version_info < (3, 10):  # noqa: UP036 - the point is the interpreter that is NOT ours
    sys.exit(
        f"this needs Python 3.10 or newer, and this is {sys.version.split()[0]}.\n"
        "Run it from mac/, where the pyproject.toml is: "
        "cd mac && uv run python poc_subsystem_timing.py"
    )

# Imported rather than restated: a second copy of this address is how the alarm
# guard in cip_xpanel.py could go stale without anyone noticing. cip_xpanel is
# stdlib-only, so this adds no tap or pyserial dependency to a script whose
# whole point is needing neither.
from cip_xpanel import AADS_HOST as HOST, PORT

# 0x12 is the slot the live lighting bridge holds, as of 2026-09-22 when it moved
# there from 0x13. Pressing an entry join there fights Home Assistant for the
# slot's one subsystem latch, so it takes a flag.
#
# 0x14 is a live wall panel, fine for probing, and where the original round-trip
# measurement was taken. Be aware that entering Lights on a slot makes the AADS
# turn on a light in that panel's own room: 0x14 is the Guest Suite and turns on
# East Hall, 0x13 was the Office and turns on North Sink. That behaviour is the
# reason the bridge moved slots.
BRIDGE_IPID = 0x12
DEFAULT_IPID = 0x14

# The home page's four subsystem-entry buttons. d93, Alarm, is in FORBIDDEN
# below and is never pressable from here.
ENTRY_JOINS = {75: "AV", 80: "Climate", 91: "Lights", 93: "Alarm"}
DEFAULT_SEQUENCE = (91, 75, 91, 75, 91)

# The DSC alarm keypad's range on the AADS, plus the alarm subsystem's own entry
# join. The bridge refuses these at table-import time and again immediately
# before bytes reach the wire; this probe refuses them too, because a probe that
# can reach a join the product cannot is a probe that can prove the wrong thing.
FORBIDDEN = frozenset(range(130, 149)) | {93}

HEARTBEAT = b"\x0d\x00\x02\x00\x00"
UPDATE_REQUEST = b"\x05\x00\x05\x00\x00\x02\x03\x00"
END_OF_QUERY_ACK = b"\x05\x00\x05\x00\x00\x02\x03\x1d"

PRESS_HOLD_SECONDS = 0.12


def registration(ipid: int) -> bytes:
    return b"\x01\x00\x0b\x00\x00\x00\x00\x00" + bytes([ipid]) + b"\x40\xff\xff\xf1\x01"


def digital_packet(join: int, pressed: bool) -> bytes:
    """Encode a digital join press or release. `join` is 1-based, the wire is 0-based."""
    n = join - 1
    body = bytes([0x00, n & 0xFF, ((n >> 8) & 0x7F) | (0x00 if pressed else 0x80)])
    payload = bytes([0x00, 0x00, len(body)]) + body
    return bytes([0x05, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


class Arrival:
    """One inbound frame, timestamped relative to whatever stimulus preceded it.

    `end_of_query` is passed in rather than inferred from the datatype. Datatype
    0x03 carries several different markers and only body byte 0x1C is the one
    that means the dump is over, so deriving it here would report an end-of-query
    that never happened.
    """

    def __init__(
        self,
        offset: float,
        ciptype: int,
        datatype: int | None,
        records: int,
        end_of_query: bool = False,
    ) -> None:
        self.offset = offset
        self.ciptype = ciptype
        self.datatype = datatype
        self.records = records
        self.is_end_of_query = end_of_query


class Window:
    """Everything that arrived after one stimulus, with the gaps between arrivals.

    The gaps are the point. A quiet-window detector resets its baseline when it
    sends the stimulus, so the threshold it needs is larger than the wait for the
    first frame as well as larger than every gap inside the dump. Recording only
    the first and last arrival, which is what the earlier round-trip run did,
    cannot tell you either number.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.arrivals: list[Arrival] = []

    @property
    def gaps(self) -> list[float]:
        """Press to first arrival, then arrival to arrival."""
        out: list[float] = []
        previous = 0.0
        for arrival in self.arrivals:
            out.append(arrival.offset - previous)
            previous = arrival.offset
        return out

    @property
    def records(self) -> int:
        return sum(a.records for a in self.arrivals)

    @property
    def end_of_query_at(self) -> float | None:
        for arrival in self.arrivals:
            if arrival.is_end_of_query:
                return arrival.offset
        return None

    def report(self) -> str:
        if not self.arrivals:
            return f"{self.label}: nothing arrived at all"
        first = self.arrivals[0].offset
        last = self.arrivals[-1].offset
        eoq = self.end_of_query_at
        eoq_text = f"end-of-query at +{eoq:.3f}s" if eoq is not None else "no end-of-query"
        return (
            f"{self.label}: {len(self.arrivals)} frames, {self.records} join records, "
            f"first +{first:.3f}s, last +{last:.3f}s, largest gap {max(self.gaps):.3f}s, "
            f"{eoq_text}"
        )


class Probe:
    def __init__(self, ipid: int, verbose: bool, raw: bool) -> None:
        self.ipid = ipid
        self.verbose = verbose
        self.raw = raw
        self.registered = False
        self.t0 = time.monotonic()
        self.window: Window | None = None
        self.window_t0 = 0.0

    def log(self, msg: str) -> None:
        print(f"[{time.monotonic() - self.t0:7.3f}] {msg}", flush=True)

    def open_window(self, label: str, stimulus_at: float) -> Window:
        self.window = Window(label)
        self.window_t0 = stimulus_at
        return self.window

    def handle(self, ciptype: int, payload: bytes, out: list[bytes]) -> None:
        now = time.monotonic()
        datatype: int | None = None
        records = 0
        end_of_query = False

        if ciptype == 0x0F:
            self.log("processor asked us to register")
            out.append(registration(self.ipid))
        elif ciptype == 0x02:
            if len(payload) == 4 and payload[:3] == b"\x00\x00\x00":
                self.registered = True
                self.log(f"registered as IP-ID 0x{self.ipid:02X} (status 0x{payload[3]:02x})")
                out.append(UPDATE_REQUEST)
            elif payload == b"\xff\xff\x02":
                sys.exit(f"IP-ID 0x{self.ipid:02X} does not exist on {HOST}")
            else:
                sys.exit(f"registration refused: {payload.hex(' ')}")
        elif ciptype == 0x03:
            sys.exit("processor disconnected us")
        elif ciptype in (0x0D, 0x0E):
            # Heartbeat traffic is ours, not the processor volunteering state.
            # Counting it as an arrival would invent gaps that a real dump does
            # not have, so it is excluded from every window below.
            return
        elif ciptype == 0x05:
            datatype = payload[3]
            body = payload[4:]
            records = self.count_records(datatype, body)
            if datatype == 0x03 and body and body[0] == 0x1C:
                end_of_query = True
                out.append(END_OF_QUERY_ACK)
        elif ciptype == 0x12:
            records = 1

        if self.raw:
            self.log(f"RX type 0x{ciptype:02x} <{payload.hex(' ')}>")

        if self.window is not None:
            offset = now - self.window_t0
            self.window.arrivals.append(Arrival(offset, ciptype, datatype, records, end_of_query))
            if self.verbose:
                kind = f"0x{ciptype:02x}"
                if datatype is not None:
                    kind += f"/0x{datatype:02x}"
                self.log(f"  +{offset:.3f}s {kind}, {records} records")

    @staticmethod
    def count_records(datatype: int, body: bytes) -> int:
        """How many joins one data frame carries. The AADS packs five per frame."""
        if datatype == 0x00:
            return max(0, len(body) // 2)
        if datatype == 0x14:
            return max(0, len(body) // 4)
        if datatype in (0x01, 0x02, 0x15):
            return 1
        return 0


def collect(
    sock: socket.socket,
    probe: Probe,
    stimulus_at: float,
    quiet: float,
    cap: float,
    release: tuple[float, bytes] | None = None,
) -> None:
    """Read frames until the link goes quiet, releasing a held join on the way.

    The release has to happen inside this loop rather than before it: the first
    frame of an entry dump has been seen at +0.063s, which is inside the 0.12s
    hold, so sleeping through the hold would misplace the arrival that matters
    most.
    """
    buf = b""
    last_arrival: float | None = None
    released = release is None
    while True:
        now = time.monotonic()
        elapsed = now - stimulus_at
        if not released and elapsed >= release[0]:
            sock.sendall(release[1])
            released = True
        if last_arrival is not None and now - last_arrival > quiet:
            return
        if elapsed > cap:
            if last_arrival is None:
                probe.log(f"  nothing arrived within {cap:.1f}s")
            return

        out: list[bytes] = []
        try:
            data = sock.recv(8192)
            if not data:
                sys.exit("connection closed by processor")
            buf += data
        except TimeoutError:
            data = b""

        while len(buf) >= 3:
            length = (buf[1] << 8) + buf[2]
            if len(buf) < length + 3:
                break
            before = len(probe.window.arrivals) if probe.window else 0
            probe.handle(buf[0], buf[3 : 3 + length], out)
            if probe.window and len(probe.window.arrivals) > before:
                last_arrival = time.monotonic()
            buf = buf[length + 3 :]
        for packet in out:
            sock.sendall(packet)


def press(sock: socket.socket, probe: Probe, join: int, quiet: float, cap: float) -> Window:
    """Tap one entry join and collect everything it shakes loose."""
    if join in FORBIDDEN:
        sys.exit(f"refusing to press d{join}: shared with the DSC alarm keypad")
    name = ENTRY_JOINS.get(join, "unknown")
    probe.log(f"pressing d{join} ({name})")
    window = probe.open_window(f"d{join} {name}", time.monotonic())
    sock.sendall(digital_packet(join, True))
    collect(
        sock,
        probe,
        probe.window_t0,
        quiet=quiet,
        cap=cap,
        release=(PRESS_HOLD_SECONDS, digital_packet(join, False)),
    )
    probe.log(f"  {window.report()}")
    return window


def recommend(windows: list[Window]) -> None:
    """The smallest quiet threshold that would have worked for every window."""
    measured = [w for w in windows if w.arrivals]
    if not measured:
        print("\nno windows produced any traffic, so there is nothing to recommend")
        return

    worst = max(max(w.gaps) for w in measured)
    worst_window = max(measured, key=lambda w: max(w.gaps))
    slowest = max(w.arrivals[-1].offset for w in measured)
    every_eoq = all(w.end_of_query_at is not None for w in measured)

    print()
    header = f"{'window':<28} {'frames':>7} {'records':>8} {'first':>8} {'last':>8}"
    print(f"{header} {'max gap':>8}  end-of-query")
    for w in measured:
        eoq = w.end_of_query_at
        print(
            f"{w.label:<28} {len(w.arrivals):>7} {w.records:>8} "
            f"{w.arrivals[0].offset:>7.3f}s {w.arrivals[-1].offset:>7.3f}s "
            f"{max(w.gaps):>7.3f}s  {f'+{eoq:.3f}s' if eoq is not None else 'none'}"
        )

    print()
    if every_eoq:
        print(
            "Every entry ended with an end-of-query frame, so async_enter() can wait for that\n"
            "marker exactly and needs no quiet threshold at all. The numbers below are the\n"
            "fallback for when it does not fire."
        )
    else:
        print(
            "At least one entry produced no end-of-query frame, so the quiet window is the\n"
            "primary signal rather than the fallback."
        )

    suggestion = max(0.05, round(worst * 2 / 0.05) * 0.05)
    print()
    print(f"largest gap seen: {worst:.3f}s, in {worst_window.label}")
    print(f"slowest dump to finish: {slowest:.3f}s")
    print(f"suggested ENTRY_QUIET_SECONDS: {suggestion:.2f} (twice the largest gap)")
    print(f"suggested ENTRY_TIMEOUT: {max(3.0, round(slowest * 3, 1)):.1f}")
    print()
    print(
        "A threshold below the largest gap would call the dump finished in the middle of it\n"
        "and enter the subsystem on partial state, which is the same silent failure as the\n"
        "2026-09-15 lighting outage: confident, complete-looking, and wrong."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--ipid", type=lambda s: int(s, 0), default=DEFAULT_IPID)
    ap.add_argument(
        "--sequence",
        default=",".join(str(j) for j in DEFAULT_SEQUENCE),
        help="entry joins to press in order (default: %(default)s, ending back in Lights)",
    )
    ap.add_argument(
        "--quiet",
        type=float,
        default=2.0,
        help="seconds of silence that end a measurement window (default: %(default)s)",
    )
    ap.add_argument(
        "--cap",
        type=float,
        default=10.0,
        help="give up on a window after this long (default: %(default)s)",
    )
    ap.add_argument(
        "--allow-bridge-slot",
        action="store_true",
        help=f"permit IP-ID 0x{BRIDGE_IPID:02X}, which Home Assistant's lighting bridge holds",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="log every frame as it arrives")
    ap.add_argument("--raw", action="store_true", help="hex-dump every frame")
    args = ap.parse_args()

    sequence = [int(part) for part in args.sequence.split(",") if part.strip()]
    for join in sequence:
        if join in FORBIDDEN:
            sys.exit(f"refusing to press d{join}: shared with the DSC alarm keypad")
        if join not in ENTRY_JOINS:
            sys.exit(f"d{join} is not a subsystem-entry join; this probe presses nothing else")

    if args.ipid == BRIDGE_IPID and not args.allow_bridge_slot:
        sys.exit(
            f"IP-ID 0x{BRIDGE_IPID:02X} is the slot Home Assistant's lighting bridge holds.\n"
            "Pressing an entry join there fights it for the slot's one subsystem latch and can\n"
            f"take every AADS load offline. Probe 0x{DEFAULT_IPID:02X} instead, or pass "
            "--allow-bridge-slot if you mean it."
        )

    probe = Probe(args.ipid, args.verbose, args.raw)
    print(
        f"This presses real buttons on slot 0x{args.ipid:02X}. If a physical panel is plugged in\n"
        "there, its display will follow: returning via d75 lands it on the A/V source menu\n"
        "rather than whatever page it was showing. Nothing else about the house changes; no\n"
        "load, zone, source or volume join is pressed.\n"
    )

    sock = socket.create_connection((args.host, PORT), timeout=5)
    sock.settimeout(0.02)
    probe.log(f"connected to {args.host}:{PORT}")

    windows: list[Window] = []
    try:
        # The registration dump is a window of its own. It is the one measurement
        # that is not an entry press, and it is the baseline the entry dumps are
        # cheap compared to: the sixty-five lighting load names arrive once per
        # session rather than once per entry.
        probe.log("waiting for the registration dump")
        probe.open_window("registration dump", time.monotonic())
        collect(sock, probe, probe.window_t0, quiet=args.quiet, cap=max(args.cap, 15.0))
        if not probe.registered:
            sys.exit("never registered; nothing below would mean anything")
        windows.append(probe.window)
        probe.log(f"  {probe.window.report()}")

        for join in sequence:
            sock.sendall(HEARTBEAT)
            windows.append(press(sock, probe, join, quiet=args.quiet, cap=args.cap))
    finally:
        sock.close()

    recommend(windows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
