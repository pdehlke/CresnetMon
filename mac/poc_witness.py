"""Ask the MC2E whether it can hear us transmitting on Cresnet.

Our RS-485 adapter is an FTDI USB-RS485 cable, and on FTDI's reference design
local echo is off by default: CBUS4 drives TXDEN, which disables the receiver
while the driver is active. So we cannot hear our own transmission, and the
"NOT found in readback" result from `living_pathway.py` proves nothing either
way. We need a witness whose receiver is not ours.

The MC2E is that witness. Its console exposes SDEBUG over the Cresnet leg:

    -DON C##      debug flag for one Cresnet ID
    -RXRON        show received packets in raw form
    -SUOFF        stop suppressing "unresolvable" packets, i.e. direct-to-wire
                  packets addressed to an ID that is not in the program

Two probes go out, neither of which commands anything:

    5A 06 5A ...  an 8-byte frame to Cresnet ID 0x5A, which does not exist.
                  0x5A has never once appeared in 937,118 bytes of capture, so
                  any 0x5A anywhere in the evidence came from us. This is the
                  probe that -SUOFF is meant to reveal.
    71 00         the routine zero-payload poll the master itself sends to the
                  dimmer at 0x71 2.2 times a second. Commands nothing, and the
                  processor is watching 0x71, so it should print this if it
                  receives it.

Detection is threefold, and only the first is conclusive:

1. The console prints a raw RX packet the processor did not transmit.
2. The bus stream shows damage. A poll to 0x62 is followed by `63 00 64 00` in
   1752 of 1757 cases in a clean capture, so any real drop in that conformance
   rate during injection means we collided with the master, which means we are
   driving the wire.
3. The error log gains entries.

A null result on all three is not proof we are off the wire: a polled master may
only enable its receiver while expecting a reply, and would then miss us.

Liveness gate: the tap can be physically unplugged with no software symptom. The
port opens, writes report success, reads return nothing, no error is raised. So
the roll call is checked before and after, and the run aborts without a verdict
if it is absent.

    uv run python poc_witness.py --dry-run
    uv run python poc_witness.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import socket
import sys
import threading
import time

import crestron_console as ccon
import serial

from cresnetmon.serial_io import PortOpenError, list_ports, open_port

# Every CLX dimmer on the main leg. Scoping to one module last time meant the
# run could not answer what happened in the rest of the house. Keypads are left
# out deliberately: they are polled at 24.2/s each and would flood the console.
CRESNET_TARGETS = [f"C0x{n:02X}" for n in range(0x70, 0x77)]

SETUP = [
    *(f"SDEBUG -DON {t}" for t in CRESNET_TARGETS),
    "SDEBUG -RXRON",
    "SDEBUG -TXRON",
    "SDEBUG -SUOFF",
    "SDEBUG -OON",
    "SDEBUG -STON",
]

# Restores the settings read off the processor before this run: raw printing off
# in both directions, unresolvable suppressed, online/offline quiet.
TEARDOWN = [
    *(f"SDEBUG -DOFF {t}" for t in CRESNET_TARGETS),
    "SDEBUG -RXROFF",
    "SDEBUG -TXROFF",
    "SDEBUG -SUON",
    "SDEBUG -OOFF",
]

PROBE_UNRESOLVED = bytes.fromhex("5A065A5A5A5A5A5A")  # 8 bytes to a nonexistent ID
PROBE_POLL = bytes.fromhex("7100")  # the master's own poll to the dimmer

ROLLCALL = 0x62  # arrives at 24.2/s in every capture, on both days, ratio 1.00
ROLLCALL_FLOOR = 20.0  # per second; the gate between "connected" and "unplugged"


def autodetect_port() -> str:
    candidates = [p for p in list_ports() if "usbserial" in p.device or "usbmodem" in p.device]
    if len(candidates) == 1:
        return candidates[0].device
    if not candidates:
        sys.exit("no USB serial adapter found; pass --port explicitly")
    listing = "\n".join(f"  {p.device}  ({p.description})" for p in candidates)
    sys.exit(f"multiple USB serial adapters found, pass --port:\n{listing}")


def conformance(buf: bytes) -> tuple[int, int]:
    """Count polls to 0x62 and how many are followed by an undamaged `63 00 64 00`."""
    total = clean = 0
    i = 0
    while i < len(buf) - 6:
        if buf[i] == ROLLCALL and buf[i + 1] == 0x00:
            total += 1
            if buf[i + 2 : i + 6] == b"\x63\x00\x64\x00":
                clean += 1
            i += 2
        else:
            i += 1
    return total, clean


class Tap:
    """Serial reader thread that timestamps every chunk it pulls off the wire."""

    def __init__(self, port) -> None:
        self.port = port
        self.chunks: list[tuple[float, bytes]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.port.read(self.port.in_waiting or 1)
            except (OSError, serial.SerialException):
                return  # port closed under us during teardown
            if data:
                self.chunks.append((time.time(), data))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def between(self, start: float, end: float) -> bytes:
        return b"".join(d for t, d in self.chunks if start <= t < end)


class Console:
    """Reader thread over the open console socket, for the SDEBUG stream."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lines: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.sock.settimeout(0.3)
        pending = ""
        while not self._stop.is_set():
            try:
                data = self.sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            pending += ccon.strip_iac(self.sock, data, None).decode("latin-1")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                self.lines.append((time.time(), line.rstrip("\r")))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def between(self, start: float, end: float) -> list[str]:
        return [ln for t, ln in self.lines if start <= t < end and ln.strip()]


def gate(tap: Tap, seconds: float, label: str) -> float:
    """Measure the roll-call rate over a window; returns polls per second."""
    start = time.time()
    time.sleep(seconds)
    seen = tap.between(start, time.time())
    rate = seen.count(bytes([ROLLCALL])) / seconds
    print(f"  {label}: 0x62 at {rate:.1f}/s over {seconds:.0f}s ({len(seen)} bytes)")
    return rate


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the MC2E if it hears our Cresnet writes.")
    parser.add_argument("--port", help="serial device (default: autodetect)")
    parser.add_argument("--baseline", type=float, default=12.0, help="quiet seconds each side")
    parser.add_argument("--inject", type=float, default=20.0, help="seconds of injection")
    parser.add_argument("--rate", type=float, default=15.0, help="bursts per second")
    parser.add_argument("--out", help="evidence file (default: captures/<ts>-witness.json)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, touch nothing")
    args = parser.parse_args()

    burst = PROBE_UNRESOLVED + PROBE_POLL
    airtime = len(burst) * 10 / 38400
    print("probes (neither commands anything):")
    print(f"  unresolved  {PROBE_UNRESOLVED.hex(' ')}   -> ID 0x5A, does not exist")
    print(f"  poll        {PROBE_POLL.hex(' ')}   -> the master's own poll to 0x71")
    print(f"\nplan: {args.baseline:.0f}s quiet / {args.inject:.0f}s inject / "
          f"{args.baseline:.0f}s quiet, {args.rate:.0f} bursts/s")
    print(f"added utilisation: {100 * airtime * args.rate:.1f}%")
    print(f"console: {ccon.HOST}:{ccon.PORT}, SDEBUG scoped to {' '.join(CRESNET_TARGETS)}")
    if args.dry_run:
        print("\ndry run, nothing touched")
        return 0

    device = args.port or autodetect_port()
    try:
        port = open_port(device)
    except PortOpenError as exc:
        sys.exit(str(exc))

    sock = socket.create_connection((ccon.HOST, ccon.PORT), timeout=8)
    ccon.drain(sock, 2.0)

    tap = Tap(port)
    tap.start()
    console = None
    try:
        print("\n=== liveness gate (before) ===")
        if gate(tap, 3.0, "tap") < ROLLCALL_FLOOR:
            sys.exit("ABORT: no roll call, the tap is not connected. No verdict possible.")

        print("\n=== processor setup ===")
        print(ccon.drain(sock, 1.0).strip() or "(quiet)")
        sock.sendall(b"CLEARERR\r\n")
        print("CLEARERR:", ccon.drain(sock, 2.0).strip().replace("\r\n", " ")[:120])
        for cmd in SETUP:
            sock.sendall(cmd.encode() + b"\r\n")
            ccon.drain(sock, 1.0)
        print(f"SDEBUG armed: {', '.join(SETUP)}")

        console = Console(sock)
        console.start()

        t_base0 = time.time()
        print(f"\n=== baseline {args.baseline:.0f}s ===")
        time.sleep(args.baseline)

        t_inj0 = time.time()
        print(f"=== injecting {args.inject:.0f}s ===")
        sent = 0
        deadline = t_inj0 + args.inject
        while time.time() < deadline:
            port.write(burst)
            port.flush()
            sent += 1
            # Jitter the interval so bursts sample every phase of the 41ms round
            # instead of beating against it at a fixed offset.
            time.sleep(random.uniform(0.5, 1.5) / args.rate)
        t_inj1 = time.time()
        print(f"  {sent} bursts, {sent * len(burst)} bytes, "
              f"{sent * len(PROBE_UNRESOLVED)} probe bytes of 0x5A")

        print(f"=== settling {args.baseline:.0f}s ===")
        time.sleep(args.baseline)
        t_end = time.time()

        print("\n=== liveness gate (after) ===")
        after = gate(tap, 3.0, "tap")

        console.stop()

        # ---- evidence 1: the processor's own receiver ----
        print("\n=== WITNESS 1: processor console ===")
        base_lines = console.between(t_base0, t_inj0)
        inj_lines = console.between(t_inj0, t_inj1)
        post_lines = console.between(t_inj1, t_end)
        print(f"  lines: baseline {len(base_lines)}  injection {len(inj_lines)}  "
              f"post {len(post_lines)}")
        for ln in inj_lines[:40]:
            print(f"    | {ln}")
        if len(inj_lines) > 40:
            print(f"    | ... {len(inj_lines) - 40} more")
        hits = [ln for ln in inj_lines if "5a" in ln.lower()]
        print(f"  lines mentioning 5A during injection: {len(hits)}")
        for ln in hits[:20]:
            print(f"    *** {ln}")

        # ---- evidence 2: damage to the master's own traffic ----
        print("\n=== WITNESS 2: bus damage ===")
        print(f"  {'window':10s} {'bytes':>8s} {'0x62':>6s} {'conformance':>12s} {'0x5A':>6s}")
        for label, a, b in (
            ("baseline", t_base0, t_inj0),
            ("injection", t_inj0, t_inj1),
            ("post", t_inj1, t_end),
        ):
            buf = tap.between(a, b)
            total, clean = conformance(buf)
            pct = 100 * clean / total if total else 0.0
            print(f"  {label:10s} {len(buf):8d} {total:6d} {pct:11.1f}% {buf.count(0x5A):6d}")

        # ---- evidence 3: the error log ----
        print("\n=== WITNESS 3: error log ===")
        sock.sendall(b"ERRLOG\r\n")
        print(ccon.drain(sock, 6.0))

        if after < ROLLCALL_FLOOR:
            print("WARNING: roll call absent on the closing gate; treat the run as void.")

        out = pathlib.Path(args.out or f"captures/{time.strftime('%Y%m%dT%H%M%S')}-witness.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "windows": {
                        "baseline": [t_base0, t_inj0],
                        "injection": [t_inj0, t_inj1],
                        "post": [t_inj1, t_end],
                    },
                    "probe": {
                        "unresolved": PROBE_UNRESOLVED.hex(" "),
                        "poll": PROBE_POLL.hex(" "),
                        "bursts": sent,
                    },
                    "sdebug_targets": CRESNET_TARGETS,
                    "bus": [[ts, chunk.hex(" ")] for ts, chunk in tap.chunks],
                    "console": [[ts, ln] for ts, ln in console.lines],
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
                ccon.drain(sock, 0.8)
            sock.sendall(b"SDEBUG -S1\r\n")
            print("\n=== settings after teardown ===")
            print(ccon.drain(sock, 3.0))
        finally:
            sock.close()
            port.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
