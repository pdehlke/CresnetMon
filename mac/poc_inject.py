"""Proof-of-concept Cresnet frame injector.

Replays the exact bytes MC2E was observed sending when the Kitchen lighting
scene was switched on and off from the Kitchen CNX-B8 keypad (`0x64`), captured
2026-08-31 into `captures/20260831T091528.jsonl` and its companion raw stream
log.

**The frames below are copied verbatim from that capture, not rebuilt from a
decode.** That is the whole point. A reconstructed frame has to get the
framing and any checksum right; a verbatim replay sidesteps both questions and
so is the shortest path to a yes/no answer on whether injection works at all.

What the raw stream established, and why this has a real chance of working:

- Cresnet framing here is bare `<dest> <size> <payload>`, with no leading
  delimiter and no trailing checksum. Parsing the 350,758-byte capture under
  that assumption yields 196,313 frames and only 70 stray bytes (0.018%).
- MC2E inserts these `1D` commands into its poll round *asynchronously*, not as
  a reply to polling the target module. An unsolicited addressed write to a CLX
  module is exactly what the master itself does.
- The frames carry a destination address but no source address. A CLX module has
  no way to distinguish an injected frame from the master's own.

Collisions are the residual risk. The bus runs a ~46.9ms poll round at ~24%
utilisation, so roughly 35ms of each round is idle, but that idle is split
across ~20 inter-frame gaps averaging under 2ms, and the longest frame here
needs 2.6ms of airtime. A blind write will therefore sometimes land on top of
the master. Two things make that acceptable: setting a channel to a level is
idempotent, so `--repeat` costs nothing, and the bus already tolerates
occasional corruption (35 two-byte glitches appeared in 374 seconds of capture
with nothing visibly breaking).

Usage:
    uv run python poc_inject.py on
    uv run python poc_inject.py off
    uv run python poc_inject.py on --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time

from cresnetmon.serial_io import PortOpenError, list_ports, open_port

# Verbatim from the 2026-08-31 capture's `raw` fields, in the order MC2E sent
# them. Each is <dest> <size> <payload>; the payload is the `1D` CLX command,
# a four-byte header followed by (channel, level) pairs.
#
#   0x72 CLX-1DIM8  ch2 -> 0x11 (dim), ch3 -> 0xFF
#   0x75 CLX-1DIM4  ch0 -> 0xFF
#   0x71 CLX-1DIM8  ch3 -> 0xFF, ch4 -> 0xFF
FRAMES: dict[str, tuple[bytes, ...]] = {
    "on": (
        bytes.fromhex("72081D0000000211 03FF".replace(" ", "")),
        bytes.fromhex("75061D00000000FF"),
        bytes.fromhex("71081D00000003FF04FF"),
    ),
    "off": (
        bytes.fromhex("72081D0000000200 0300".replace(" ", "")),
        bytes.fromhex("75061D0000000000"),
        bytes.fromhex("71081D0000000300 0400".replace(" ", "")),
    ),
}

# MC2E emitted the three commands back to back inside one poll round. Matching
# that spacing keeps the injected burst looking like the real thing; it is not
# known to be required.
INTER_FRAME_SECONDS = 0.002

# Gap between repeats of the whole burst. One poll round is ~46.9ms, so this
# lands each retry in a different phase of the round rather than hammering the
# same instant the master is transmitting.
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
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=sorted(FRAMES), help="which captured burst to replay")
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
        help="after injecting, read the bus and report bytes seen (0 to skip)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print frames without transmitting")
    args = parser.parse_args()

    frames = FRAMES[args.action]

    print(f"action: {args.action}   frames: {len(frames)}   repeats: {args.repeat}")
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
            # A half-duplex RS-485 adapter usually hears its own transmission,
            # so finding an injected frame in this readback is direct evidence
            # the bytes reached the wire, independent of whether the lights moved.
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
