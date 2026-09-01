"""Turn the Living Pathway light on or off by replaying captured Cresnet frames.

The bytes below are copied verbatim from `captures/20260901T111751.jsonl`, the
labeled on/off pair recorded 2026-09-01 while the Living Pathway button was
pressed. They are not rebuilt from a decode, for the same reason `poc_inject.py`
isn't: a verbatim replay can't get the framing wrong.

**That capture's embedded device label is wrong.** It records `0x6F` / Kitchen;
the press was actually on `0x6A`, the Great Room keypad, confirmed by the owner
and consistent with the program descriptor (`Slot-01.ID-6A ... 105-Great Room`,
`Slot-01.ID-6F ... 101-Kitchen`). The frames themselves are unaffected: they are
addressed to the dimmer modules and carry no source address. Only the
provenance note was wrong.

Living Pathway is two dimmer channels driven together, so each action is a pair
of frames MC2E sent back to back inside a single poll round:

    0x70  ch4 -> 0xC3 on, 0x00 off
    0x71  ch3 -> 0xC3 on, 0x00 off

0xC3 rather than 0xFF: the button is programmed to a preset level (~76%), not
full. Off is a true 0x00 on both channels.

Whether this actually works is still the open question from 2026-08-31. Writes
are blind, the master owns the bus, and a frame that lands on top of a poll is
lost. Setting a channel to a level is idempotent, so `--repeat` is free and each
retry lands in a different phase of the ~47ms round.

Usage:
    uv run python living_pathway.py on
    uv run python living_pathway.py off
    uv run python living_pathway.py on --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time

from cresnetmon.serial_io import PortOpenError, list_ports, open_port

# Verbatim `raw` fields from the capture, in the order MC2E sent them.
# Each frame is <dest> <size> <payload>; payload is the `1D` CLX set-level
# command, a four-byte header followed by (channel, level) pairs.
FRAMES: dict[str, tuple[bytes, ...]] = {
    "on": (
        bytes.fromhex("70061D00000004C3"),
        bytes.fromhex("71061D00000003C3"),
    ),
    "off": (
        bytes.fromhex("70061D0000000400"),
        bytes.fromhex("71061D0000000300"),
    ),
}

# The two frames arrived within one 16ms sample of each other, i.e. back to
# back in the same poll round. 2ms is one frame's airtime plus slack.
INTER_FRAME_SECONDS = 0.002

# One poll round is ~47ms. Retrying at 37ms walks the burst around the round
# instead of hammering the same instant the master transmits.
INTER_REPEAT_SECONDS = 0.037


def autodetect_port() -> str:
    """Return the sole USB serial device, or exit with the candidates listed."""
    candidates = [p for p in list_ports() if "usbserial" in p.device or "usbmodem" in p.device]
    if len(candidates) == 1:
        return candidates[0].device
    if not candidates:
        sys.exit("no USB serial adapter found; pass --port explicitly")
    listing = "\n".join(f"  {p.device}  ({p.description})" for p in candidates)
    sys.exit(f"multiple USB serial adapters found, pass --port:\n{listing}")


def describe(frame: bytes) -> str:
    """Render a frame as hex plus its decoded (channel, level) pairs."""
    dest, size, payload = frame[0], frame[1], frame[2:]
    pairs = " ".join(f"ch{payload[i]}={payload[i + 1]:#04x}" for i in range(4, len(payload), 2))
    return f"{frame.hex(' ')}   -> {dest:#04x} size={size} {pairs}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch the Living Pathway light on or off.")
    parser.add_argument("action", choices=sorted(FRAMES))
    parser.add_argument("--port", help="serial device (default: autodetect)")
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="times to replay the burst; idempotent, raises the odds one lands in an idle gap",
    )
    parser.add_argument(
        "--listen",
        type=float,
        default=1.5,
        metavar="SECONDS",
        help="after injecting, read the bus back and report what was seen (0 to skip)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print frames without transmitting")
    args = parser.parse_args()

    frames = FRAMES[args.action]
    print(f"Living Pathway {args.action}   frames: {len(frames)}   repeats: {args.repeat}")
    for frame in frames:
        print(f"  {describe(frame)}")

    if args.dry_run:
        print("\ndry run, nothing transmitted")
        return 0

    device = args.port or autodetect_port()
    print(f"\nopening {device} at 38400 8N1")
    try:
        port = open_port(device)
    except PortOpenError as exc:
        sys.exit(str(exc))

    try:
        port.reset_input_buffer()
        for attempt in range(1, args.repeat + 1):
            for frame in frames:
                port.write(frame)
                port.flush()
                time.sleep(INTER_FRAME_SECONDS)
            print(f"  burst {attempt}/{args.repeat} written")
            if attempt < args.repeat:
                time.sleep(INTER_REPEAT_SECONDS)

        if args.listen > 0:
            # A half-duplex adapter normally hears its own transmission, so
            # finding a frame here is evidence the bytes reached the wire -
            # separate from whether the light moved.
            print(f"\nlistening {args.listen}s...")
            deadline = time.time() + args.listen
            seen = bytearray()
            while time.time() < deadline:
                seen += port.read(port.in_waiting or 1)
            print(f"  {len(seen)} bytes read")
            for frame in frames:
                verdict = "found" if frame in seen else "NOT found"
                print(f"  {frame.hex(' ')}: {verdict} in readback")
    finally:
        port.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
