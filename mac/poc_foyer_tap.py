"""Passive Cresnet capture for a keypad button with no CIP-visible join.

The Foyer keypad's real "Home Perimeter" button was already ruled out on both
CIP links: watching the crestron_cip bridge's own raw digital-join trace while
it was pressed showed nothing but the Goodbye/Good Night "everything's off"
tell moving, never a join of its own. So this does not register on CIP or talk
to any processor console at all, it only reads the wire, the same `Tap` used in
poc_witness.py to independently confirm a physical press when nothing else can
be trusted yet.

No SDEBUG leg here unlike poc_joinwatch.py: that script's SDEBUG target
(`E03`) is specific to the MC2E's XPanel slot from the original Kitchen
investigation, and there is no reason yet to believe this button's traffic
touches that processor's console at all. Bus traffic only, analyzed after the
fact.

    uv run python poc_foyer_tap.py --seconds 300

Prints a per-second summary of which Cresnet destination IDs appeared, and
flags any ID that is not part of the steady polling background (present in
nearly every second of the whole capture) as worth a closer look.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter

from poc_witness import ROLLCALL_FLOOR, Tap, autodetect_port, gate

from cresnetmon.serial_io import PortOpenError, open_port


def frames(buf: bytes) -> list[tuple[int, bytes]]:
    """Split a raw buffer into (dest, payload) pairs, best-effort.

    Same simple framing `notable()` in poc_joinwatch.py uses: one byte of
    destination ID, one byte of payload size, then the payload. Frame
    boundaries can still land wrong across a chunk split; that is why this
    looks at second-wide buckets rather than trusting any single frame.
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    while i < len(buf) - 1:
        dest, size = buf[i], buf[i + 1]
        if i + 2 + size > len(buf):
            break
        out.append((dest, buf[i + 2 : i + 2 + size]))
        i += 2 + size if size else 2
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Passively watch Cresnet for a keypad press.")
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--port", help="serial device (default: autodetect)")
    ap.add_argument("--out", default="captures/foyer-tap.jsonl")
    args = ap.parse_args()

    try:
        port = open_port(args.port or autodetect_port())
    except PortOpenError as exc:
        sys.exit(str(exc))

    tap = Tap(port)
    tap.start()
    if gate(tap, 3.0, "tap") < ROLLCALL_FLOOR:
        sys.exit("ABORT: no roll call, the tap is not connected.")

    t0 = time.time()
    print(
        f"LISTENING {args.seconds:.0f}s on the Cresnet tap only. "
        "Press the keypad now, whenever you're ready.",
        flush=True,
    )
    while time.time() - t0 < args.seconds:
        time.sleep(1.0)

    gate(tap, 2.0, "tap (post-capture)")
    tap.stop()

    with open(args.out, "w") as fh:
        for t, data in tap.chunks:
            fh.write(json.dumps({"t": t, "hex": data.hex()}) + "\n")
    print(f"saved {len(tap.chunks)} chunks to {args.out}")

    # Per-second destination-ID sets, then flag anything outside the steady
    # polling background (present in almost every second of the whole run).
    buckets: dict[int, set[int]] = {}
    for t, data in tap.chunks:
        sec = int(t - t0)
        buckets.setdefault(sec, set()).update(dest for dest, _ in frames(data))

    presence = Counter()
    for ids in buckets.values():
        presence.update(ids)
    total_seconds = max(buckets) + 1 if buckets else 0
    background = {dest for dest, n in presence.items() if n >= 0.8 * total_seconds}

    print(f"\nbackground IDs (in >=80% of {total_seconds}s): "
          f"{sorted(hex(d) for d in background)}")
    print("\nsecond-by-second, anything outside that background:")
    for sec in sorted(buckets):
        extra = buckets[sec] - background
        if extra:
            print(f"  t+{sec:3d}s: {sorted(hex(d) for d in extra)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
