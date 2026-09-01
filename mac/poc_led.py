"""Settle whether this adapter transmits, by blinking a keypad LED.

Every statistical probe so far has been ambiguous, because the adapter does not
echo its own transmission and heavy writes perturb reception on the shared FTDI
endpoint. This test removes the measurement problem entirely: it drives a keypad
LED and you watch it.

Why this is safe. `00 <index> <state>` addressed to a CNX-B8 is display-only
feedback, the same frame family MC2E emits after every lighting change. It
actuates nothing and touches no lighting load. The worst case is a keypad
showing a stale LED until the next real event re-asserts it, and this script
restores the original state on the way out.

State polarity is active-low, established from the 2026-08-31 capture: `00` lights
the LED, `80` darkens it. Button 7 is "Good Bye" on the five keypads that carry
it, and it is lit whenever the house is all-off, which makes it the most visible
target to toggle.

Collisions are handled by repetition rather than timing. A 5-byte frame is 1.3ms
of airtime against inter-frame gaps averaging under 2ms, so any single write may
well be stepped on; blasting the frame for the whole half-cycle makes that
irrelevant.

    uv run python poc_led.py                          # Great Room, 30s of blinking
    uv run python poc_led.py --device 0x67 --delay 20  # Foyer, after a 20s walk
"""

from __future__ import annotations

import argparse
import sys
import time

from cresnetmon.serial_io import PortOpenError, list_ports, open_port

LIT, DARK = 0x00, 0x80


def led_frame(device: int, button: int, state: int) -> bytes:
    """Build `<dest> <size=3> 00 <button> <state>`, the observed LED format."""
    return bytes([device, 0x03, 0x00, button, state])


def blast(port, frame: bytes, seconds: float, rts: bool | None) -> tuple[int, int]:
    """Write one frame repeatedly for a window. Returns (frames, bytes written).

    `rts` selects direction-control handling. None leaves RTS alone, correct if
    the FT232RL's CBUS is programmed as TXDEN (automatic, the usual design for
    an FTDI RS485 cable). True/False assert that level around each individual
    write and drain the UART before releasing, which is what an RTS-controlled
    transceiver needs, and what the earlier probe got wrong by holding RTS for
    whole three-second phases instead of per frame.
    """
    deadline = time.time() + seconds
    frames = written = 0
    while time.time() < deadline:
        if rts is not None:
            port.rts = rts
        written += port.write(frame) or 0
        if rts is not None:
            port.flush()  # tcdrain: bytes must clear the UART before releasing DE
            port.rts = not rts
        frames += 1
        time.sleep(0.004)
    return frames, written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", help="serial device (default: autodetect)")
    parser.add_argument(
        "--device",
        default="0x6A",
        help="keypad Cresnet id (default 0x6A, Great Room, the documented tap point)",
    )
    parser.add_argument("--button", type=int, default=7, help="button index (default 7, Good Bye)")
    parser.add_argument(
        "--cycles", type=int, default=15, help="on/off cycles (default 15 = 30s at 1s hold)"
    )
    parser.add_argument(
        "--rts-mode",
        choices=("auto", "none", "high", "low"),
        default="auto",
        help="direction control; auto runs all three in turn (default)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to wait before blinking, so you can walk to the keypad first",
    )
    parser.add_argument("--hold", type=float, default=1.0, help="seconds per half-cycle")
    parser.add_argument(
        "--final",
        choices=("lit", "dark"),
        default="lit",
        help="state to leave the LED in (default lit, the all-off resting state)",
    )
    args = parser.parse_args()

    device = int(args.device, 16)
    port_path = args.port
    if not port_path:
        found = [p for p in list_ports() if "usbserial" in p.device or "usbmodem" in p.device]
        if len(found) != 1:
            sys.exit("pass --port explicitly; found: " + ", ".join(p.device for p in found))
        port_path = found[0].device

    lit = led_frame(device, args.button, LIT)
    dark = led_frame(device, args.button, DARK)
    print(f"port    {port_path} @38400 8N1")
    print(f"target  keypad {device:#04x} button {args.button}")
    print(f"lit     {lit.hex(' ')}")
    print(f"dark    {dark.hex(' ')}")
    total = args.cycles * args.hold * 2
    print(
        f"\nWATCH THE KEYPAD. {args.cycles} cycles, {args.hold}s per state, {total:.0f}s total.\n"
    )

    modes: list[tuple[str, bool | None]] = (
        [("none (TXDEN auto)", None), ("RTS high", True), ("RTS low", False)]
        if args.rts_mode == "auto"
        else [
            {
                "none": ("none (TXDEN auto)", None),
                "high": ("RTS high", True),
                "low": ("RTS low", False),
            }[args.rts_mode]
        ]
    )

    try:
        port = open_port(port_path)
    except PortOpenError as exc:
        sys.exit(str(exc))

    try:
        if args.delay > 0:
            for remaining in range(int(args.delay), 0, -1):
                print(f"  starting in {remaining}s...", end="\r", flush=True)
                time.sleep(1)
            print(" " * 30, end="\r")
        for label, rts in modes:
            print(f"\n--- direction mode: {label} ---", flush=True)
            tf = tb = 0
            for n in range(1, args.cycles + 1):
                f1, b1 = blast(port, dark, args.hold, rts)
                f2, b2 = blast(port, lit, args.hold, rts)
                tf += f1 + f2
                tb += b1 + b2
                print(f"  cycle {n}/{args.cycles}", end="\r", flush=True)
            print(f"  {tf} frames, {tb} bytes written to the port       ")
        blast(port, lit if args.final == "lit" else dark, 0.3, None)
        print(f"\nrestored to {args.final}")
    finally:
        port.close()

    print(
        "\nIf the LED visibly blinked, this adapter transmits and CNX-B8 devices\n"
        "accept our frames. The injection problem is then MC2E correcting us, or\n"
        "the CLX modules specifically, not the wire.\n"
        "If nothing moved, nothing is reaching the bus: re-check A+/B- and whether\n"
        "this adapter needs hardware direction control."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
