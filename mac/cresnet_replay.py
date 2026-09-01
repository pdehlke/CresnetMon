"""Replay captured Cresnet events onto the live bus, and prove whether the
adapter transmits at all.

Built from scratch against two files and nothing else:

* `captures/20260831T110421.jsonl` - two labelled bursts, pde switching the
  Pool Bath light on and then off from a touchpanel.
* `captures/20260831T110355-raw.jsonl` - the raw byte stream underneath them.

What those two files actually say
---------------------------------
Framing is `<dest> <len> <payload>`, back to back, no delimiter and no
checksum. Re-derived here, not taken on trust: parsing 815,866 bytes of the
earlier raw capture that way yields 406,975 frames and 43 unexplained bytes,
0.005%.

The bus is master-polled. MC2E (`0x02`) sends `<dev> 00`; the device answers
`02 00`, which is the same framing - a zero-length frame addressed back to the
master. Keypads `62 63 64 65 66 67 6A 6D 6F` are polled every round, ~24
rounds/s; dimmer modules `70`-`76` get one slot per round on a rotation, so
each sees ~2.2 polls/s.

The whole Pool Bath "on" event is one frame:

    72 06 1D 00 00 00 06 FF     -> module 0x72, channel 6, level 0xFF

and "off" is the same frame with the level zeroed. The four `00 07 xx` frames
that follow each event are MC2E repainting the "Good Bye" LED on keypads
62/66/67/6F; they light nothing and are replayed only with `--with-leds`.

Why this might work where the master's own frames are indistinguishable from
ours: frames carry a destination but no source, and MC2E inserts these `1D`
commands into its poll round asynchronously rather than as a reply to polling
the target. An unsolicited addressed write is exactly what the master does.

The measurement problem, and how `selftest` gets round it
---------------------------------------------------------
Five earlier attempts failed to establish whether this adapter transmits at
all, and they failed for a structural reason rather than a careless one. Every
one of them asked the question through our own receive path: does our frame
echo back, does an injected poll draw a reply. This adapter goes deaf while and
after it writes - measured, reception drops by three quarters while writing two
bytes every 50ms - so a reply to our own poll arrives inside the deaf window by
construction. No amount of care with the counting could have worked.

`selftest` asks the bus instead. MC2E re-establishes a module it thinks has
dropped off the wire and announces it with a `1C` channel map addressed to that
module. On a quiet bus it never does this. Injecting inert polls of a vacant
address makes it start, and stopping makes it stop, with quiet windows either
side so the effect has to appear *and* disappear. Measured here: 0 in 60s
quiet, 49 in 60s injecting at 2/s, 0 in 60s quiet again.

That answers the question - the bytes reach the pair - and it keeps working
when our own reception is too corrupt to parse, which is exactly the condition
under which the older tests were run.

What it does not mean
---------------------
Disturbing the bus is not the same as being understood by it. Each frame we
send blocks the wire long enough for MC2E to lose a device: single, widely
spaced frames provoke a re-initialisation almost every time, which is far more
damage than an 8-byte collision can account for. MC2E's recovery re-asserts
every channel it owns, and its idea of Pool Bath is off, so an injection that
did land gets overwritten seconds later by the master putting things back.
`state` shows those assertions.

So the honest position is: transmission works, clean injection does not, and
the obstacle is this adapter holding the line far longer than the data needs.

Health first
------------
`health` gates everything else, and it exists because of how this session went
wrong. A tap with a marginal conductor does not fail loudly. It keeps
delivering bytes, and the bytes keep containing recognisable `02 00` replies,
so a corrupt window reads as a working one right up until you count the address
bytes that are missing. Run `health` before believing any other number here.

    uv run python cresnet_replay.py health
    uv run python cresnet_replay.py listen --seconds 20
    uv run python cresnet_replay.py selftest
    uv run python cresnet_replay.py state --nudge
    uv run python cresnet_replay.py on
"""

import argparse
import json
import statistics
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import serial
from serial.tools import list_ports as _list_ports

BAUD = 38400
BYTE_SECONDS = 10 / BAUD  # 8N1: start + 8 data + stop

MASTER = 0x02
# Address space the parser will sync on. The captures only ever show 0x62-0x76,
# but probes deliberately address vacant slots, and those have to parse too or
# an echo of our own probe would be scored as damage.
DEVICE_ADDRS = frozenset(range(0x60, 0x80))
SYNC_ADDRS = DEVICE_ADDRS | {MASTER}

# A poll reply is `02 00`. The captures also show `03/82/83` in byte 0 and `80`
# in byte 1 on whichever single device is currently "chatty" - 0x66 in the
# 09:14 capture, 0x6A in the 11:03 one. Unexplained, but they sit exactly where
# a reply belongs and accepting them drops unexplained bytes from 0.149% to
# 0.005%, so they are treated as replies rather than as noise.
REPLY_HEAD = frozenset({0x02, 0x03, 0x82, 0x83})
REPLY_TAIL = frozenset({0x00, 0x80})

# Verbatim from captures/20260831T110421.jsonl, `raw` fields.
POOL_BATH_ON = bytes.fromhex("72061D00000006FF")
POOL_BATH_OFF = bytes.fromhex("72061D0000000600")
# MC2E's own follow-up: "Good Bye" LED (button 7) dark when something is lit,
# lit when the house is all-off. Display only.
LEDS_ON = tuple(bytes([d, 0x03, 0x00, 0x07, 0x80]) for d in (0x66, 0x62, 0x6F, 0x67))
LEDS_OFF = tuple(bytes([d, 0x03, 0x00, 0x07, 0x00]) for d in (0x66, 0x62, 0x6F, 0x67))

CONTROL_ADDR = 0x77  # never polled, no device seen in any capture

# `1C` is MC2E re-establishing a module it thinks dropped off the bus: two
# header bytes then one (channel, on/off) pair per channel, ascending from 0.
# Distinct from `1D`, which sets one channel to an 8-bit level. Never seen on a
# quiet bus - it appears only during recovery, which is what makes it a usable
# transmit detector and the only state readback the protocol offers.
REINIT_OPCODE = 0x1C


# --------------------------------------------------------------------------
# Frame parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    offset: int
    kind: str  # "poll" | "reply" | "data" | "stray"
    addr: int
    payload: bytes
    orphan: bool = False


def parse(stream: bytes) -> list[Frame]:
    """Resynchronising walk of a sniffed byte stream.

    Deliberately not a state machine fed byte by byte. A sniffer joins the bus
    mid-frame and takes occasional bit errors, so the parser has to be able to
    give up on one byte and try again at the next - which is far easier to get
    right, and to test, over a whole buffer than incrementally.
    """
    out: list[Frame] = []
    i = 0
    previous = ""
    while i + 1 < len(stream):
        b0, b1 = stream[i], stream[i + 1]
        if b0 in DEVICE_ADDRS and b1 == 0x00:
            out.append(Frame(i, "poll", b0, b""))
            previous = "poll"
            i += 2
        elif b0 in REPLY_HEAD and b1 in REPLY_TAIL:
            out.append(Frame(i, "reply", MASTER, stream[i : i + 2], orphan=previous != "poll"))
            previous = "reply"
            i += 2
        elif b0 in SYNC_ADDRS and 0 < b1 <= 32 and i + 2 + b1 <= len(stream):
            out.append(Frame(i, "data", b0, stream[i + 2 : i + 2 + b1]))
            previous = "data"
            i += 2 + b1
        else:
            out.append(Frame(i, "stray", b0, stream[i : i + 1]))
            previous = "stray"
            i += 1
    return out


def decode_channel_map(payload: bytes) -> list[tuple[int, int]] | None:
    """Decode a `1C` module state assertion, or None if this is not one.

    Layout is `1C <flag>` then (channel, on/off) pairs. The channel numbers are
    required to run 0, 1, 2, ... with no gaps, which is what makes this safe to
    run over a corrupt stream: random bytes essentially never produce an
    ascending run, so a decode that succeeds is almost certainly a real frame.
    """
    if len(payload) < 4 or payload[0] != REINIT_OPCODE:
        return None
    body = payload[2:]
    if len(body) % 2:
        return None
    pairs = [(body[i], body[i + 1]) for i in range(0, len(body), 2)]
    if [channel for channel, _ in pairs] != list(range(len(pairs))):
        return None
    return pairs


@dataclass
class Stats:
    """Everything one observation window has to say."""

    seconds: float = 0.0
    total_bytes: int = 0
    polls: Counter[int] = field(default_factory=Counter)
    replies: int = 0
    orphans: int = 0
    data_frames: list[tuple[int, bytes]] = field(default_factory=list)
    strays: int = 0

    @property
    def poll_total(self) -> int:
        return sum(self.polls.values())

    @property
    def stray_pct(self) -> float:
        return 100.0 * self.strays / self.total_bytes if self.total_bytes else 0.0

    @property
    def orphans_per_second(self) -> float:
        return self.orphans / self.seconds if self.seconds else 0.0

    @property
    def rounds_per_second(self) -> float:
        """Poll rounds seen per second. A healthy bus runs ~24."""
        return self.polls[0x62] / self.seconds if self.seconds else 0.0

    def health(self) -> list[str]:
        """Reasons this window is too corrupt to draw conclusions from.

        The point of failure in every earlier attempt at this was trusting a
        measurement taken through a bad tap. A sniffer with one marginal
        conductor still delivers plausible-looking bytes - `02 00` survives
        because it is mostly idle bits - while silently losing most of the
        address bytes, which is exactly the data every count here depends on.
        Empty list means the window is trustworthy.
        """
        problems = []
        if self.stray_pct > 2.0:
            problems.append(f"{self.stray_pct:.1f}% of bytes unparseable (healthy: under 1%)")
        if self.rounds_per_second < 15.0:
            problems.append(f"{self.rounds_per_second:.1f} poll rounds/s (healthy: ~24)")
        if self.poll_total and self.replies / self.poll_total < 0.70:
            problems.append(
                f"only {100 * self.replies / self.poll_total:.0f}% of polls answered"
                " (healthy: ~89%, one keypad is silent)"
            )
        if not self.poll_total:
            problems.append("no polls recognised at all")
        return problems

    def line(self) -> str:
        return (
            f"{self.total_bytes:6d} B  "
            f"polls {self.poll_total:5d}  "
            f"replies {self.replies:5d}  "
            f"orphans {self.orphans:4d} ({self.orphans_per_second:5.1f}/s)  "
            f"stray {self.stray_pct:5.2f}%"
        )


def summarise(stream: bytes, seconds: float) -> Stats:
    s = Stats(seconds=seconds, total_bytes=len(stream))
    for f in parse(stream):
        if f.kind == "poll":
            s.polls[f.addr] += 1
        elif f.kind == "reply":
            s.replies += 1
            s.orphans += f.orphan
        elif f.kind == "data":
            s.data_frames.append((f.addr, f.payload))
        else:
            s.strays += 1
    return s


# --------------------------------------------------------------------------
# Bus I/O
# --------------------------------------------------------------------------


class BusMonitor:
    """Background reader that keeps timestamped chunks of the byte stream.

    Timestamps are taken the moment `read()` returns its first byte, which is
    as close to arrival as this adapter allows: the FTDI latency timer batches
    deliveries into ~16ms chunks regardless of what we do here.
    """

    def __init__(self, port: serial.Serial, raw_log: Path | None = None) -> None:
        self.port = port
        self._chunks: list[tuple[float, bytes]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._raw = raw_log.open("a") if raw_log else None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._raw:
            self._raw.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self.port.read(1)
                if not first:
                    continue
                now = time.monotonic()
                waiting = self.port.in_waiting
                data = first + (self.port.read(waiting) if waiting else b"")
            except OSError:  # SerialException subclasses OSError; port closed under us
                return
            with self._lock:
                self._chunks.append((now, data))
            if self._raw:
                self._raw.write(json.dumps({"t": time.time(), "hex": data.hex(" ").upper()}) + "\n")

    def window(self, start: float, end: float) -> bytes:
        """Bytes whose chunk timestamp falls in [start, end)."""
        with self._lock:
            return b"".join(d for t, d in self._chunks if start <= t < end)

    def chunk_gaps(self, start: float, end: float) -> list[float]:
        with self._lock:
            times = [t for t, _ in self._chunks if start <= t < end]
        return [b - a for a, b in zip(times, times[1:], strict=False)]


class Injector:
    """Writes frames, with optional hardware direction control.

    `mode` names the handshake line and the level asserted while transmitting.
    "none" assumes the FT232RL's CBUS is programmed as TXDEN, which is the
    stock arrangement for an FTDI RS485 cable and needs no software help. The
    rts/dtr modes cover adapters that expect the driver enable to be steered
    over a modem control line, and drain the UART before releasing it so the
    last byte is not cut off mid-shift.
    """

    MODES = ("none", "rts-high", "rts-low", "dtr-high", "dtr-low")

    def __init__(self, port: serial.Serial, mode: str = "none") -> None:
        if mode not in self.MODES:
            raise ValueError(f"unknown direction mode {mode!r}")
        self._port = port
        self._mode = mode
        self.frames_written = 0
        self.bytes_written = 0

    def send(self, frame: bytes) -> int:
        line, level = self._control()
        if line:
            setattr(self._port, line, level)
        written = self._port.write(frame) or 0
        if line:
            self._port.flush()  # tcdrain; the driver must stay on until it clears
            setattr(self._port, line, not level)
        self.frames_written += 1
        self.bytes_written += written
        return written

    def _control(self) -> tuple[str | None, bool]:
        if self._mode == "none":
            return None, False
        line, level = self._mode.split("-")
        return line, level == "high"


def resolve_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = [p.device for p in _list_ports.comports() if "usbserial" in p.device]
    if len(found) != 1:
        sys.exit("pass --port explicitly; candidates: " + (", ".join(found) or "none"))
    return found[0]


def open_bus(path: str) -> serial.Serial:
    # timeout is what makes the reader thread interruptible; the monitor polls
    # its stop flag between reads rather than being killed mid-read.
    try:
        return serial.Serial(path, BAUD, timeout=0.2)
    except serial.SerialException as exc:
        sys.exit(f"failed to open {path}: {exc}")


def observe(
    monitor: BusMonitor,
    seconds: float,
    *,
    injector: Injector | None = None,
    frames: tuple[bytes, ...] = (),
    interval: float = 0.05,
    label: str = "",
) -> Stats:
    """Run one observation window, optionally injecting while it runs."""
    if label:
        print(f"  {label:<28}", end="", flush=True)
    start = time.monotonic()
    deadline = start + seconds
    if injector and frames:
        nxt = start
        while time.monotonic() < deadline:
            for f in frames:
                injector.send(f)
            nxt += interval
            time.sleep(max(0.0, nxt - time.monotonic()))
    else:
        time.sleep(seconds)
    end = time.monotonic()
    # The FTDI batches deliveries by up to 16ms, so bytes belonging to this
    # window can land just after it. Wait one latency period before slicing.
    time.sleep(0.05)
    stats = summarise(monitor.window(start, end), end - start)
    if label:
        print(stats.line())
    return stats


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace, monitor: BusMonitor, injector: Injector) -> int:
    """Is the tap good enough to believe anything else this tool says?

    Run this first, every time. A loose conductor at the tap does not announce
    itself: reception keeps producing bytes, and the bytes keep containing
    recognisable `02 00` replies, so a corrupt window looks like a working one
    until you count what is missing.
    """
    deadline = time.monotonic() + args.wait
    while True:
        stats = observe(monitor, args.seconds, label="sample")
        problems = stats.health()
        if not problems or time.monotonic() >= deadline:
            break
        print(f"    unhealthy, retrying (waiting up to {args.wait:.0f}s)")
    print()
    if not problems:
        print("  HEALTHY. Counts from this tap can be trusted.")
        return 0
    print("  UNHEALTHY. Do not trust any transmit result taken through this tap:")
    for p in problems:
        print(f"    - {p}")
    print()
    print("  Reception is a two-wire differential measurement referenced to ground.")
    print("  Losing any one of the three conductors leaves it working badly rather")
    print("  than not at all, which is what this looks like. Check the screw")
    print("  terminals at the tap - A+, B-, and especially GND - and re-run.")
    return 2


def cmd_listen(args: argparse.Namespace, monitor: BusMonitor, injector: Injector) -> int:
    print(f"listening for {args.seconds:.0f}s\n")
    stats = observe(monitor, args.seconds, label="bus")
    print()
    print(f"  rounds/s      {stats.polls[0x62] / stats.seconds:.1f}")
    print(f"  utilisation   {100 * stats.total_bytes * BYTE_SECONDS / stats.seconds:.1f}%")
    print("  polls seen    " + " ".join(f"{a:02X}:{n}" for a, n in sorted(stats.polls.items())))
    missing = stats.poll_total - stats.replies
    print(f"  unanswered    {missing} polls ({100 * missing / max(1, stats.poll_total):.1f}%)")
    gaps = monitor.chunk_gaps(0, time.monotonic())
    if gaps:
        print(f"  read latency  median {statistics.median(gaps) * 1e3:.1f}ms")
    if stats.data_frames:
        print("  data frames:")
        for addr, payload in stats.data_frames:
            print(f"    {addr:02X} {payload.hex(' ').upper()}")
    return 0


def cmd_selftest(args: argparse.Namespace, monitor: BusMonitor, injector: Injector) -> int:
    """Does this adapter transmit? Measured on the bus, not on our own receiver.

    Every earlier attempt asked the question through our own receive path -
    echo, or a reply to an injected poll - and every one was inconclusive,
    because this adapter goes deaf while and after it writes. A reply to our
    own poll arrives inside that deaf window by construction, so no amount of
    care with the counting could have worked.

    This asks the bus instead. MC2E re-initialises a module it thinks fell off
    the wire, and says so with a `1C` channel map addressed to that module. In
    quiet conditions it never does this. If it starts doing it while we write
    and stops when we stop, our bytes are reaching the pair - and that holds
    even when our own reception is too corrupt to parse, which is exactly when
    the older tests failed.

    Quiet windows either side make it a dose-response measurement rather than a
    single reading: the effect has to appear and then disappear.
    """
    payload = bytes([args.control, 0x00])
    print(
        f"stimulus  {payload.hex(' ').upper()}  poll of {args.control:#04x},"
        f" a vacant address - inert if it lands\n"
        f"response  MC2E `1C` module re-initialisations, counted on the wire\n"
        f"windows   {args.window:.0f}s each, quiet / injecting / quiet\n"
    )

    before = _reinit_window(monitor, args.window, None, "quiet (before)")
    inj = Injector(monitor.port, "none" if args.rts_mode == "auto" else args.rts_mode)
    during = _reinit_window(monitor, args.window, (inj, payload, args.interval), "injecting")
    after = _reinit_window(monitor, args.window, None, "quiet (after)")

    print()
    print("=" * 74)
    # `before` is the baseline, not `before + after`. MC2E's recovery outlives
    # the stimulus by tens of seconds, so the trailing window measures how the
    # effect decays, not how often it happens by chance. Only the quiet window
    # *preceding* the injection can bound the false-positive rate, because
    # nothing MC2E does there can have been caused by a write we had not made.
    transmitting = during >= 4 and during > 3 * (before + 1)
    if transmitting:
        print("  VERDICT: TRANSMITTING.")
        print(
            f"  {before} re-initialisations in {args.window:.0f}s of quiet, then {during}"
            f" while writing {inj.frames_written} frames."
        )
        print(f"  {after} more as it settled afterwards. Our bytes reach the bus.")
        print()
        print("  Note what this does *not* say. Disturbing the bus is not the same as")
        print("  being understood by it: each frame blocks the wire long enough for")
        print("  MC2E to lose a device, and its recovery re-asserts every channel it")
        print("  owns - including whichever one you were trying to set. See `state`.")
    else:
        print("  VERDICT: no measurable effect on the bus.")
        print(f"  quiet {before}, injecting {during}, settling {after}.")
        print()
        print("  Either the RS-485 driver never enables or its output is not reaching")
        print("  the pair. Software has now said all it can: measure DC volts across")
        print("  A+/B- during a burst, or watch from a second adapter.")
    return 0 if transmitting else 1


def _reinit_window(
    monitor: BusMonitor,
    seconds: float,
    inject: tuple[Injector, bytes, float] | None,
    label: str,
) -> int:
    """One dose-response window; returns the re-initialisation count.

    Counted as a raw byte pattern rather than through `parse`, deliberately.
    The whole value of this test is that it still works when the tap is too
    corrupt to frame, and frame-level counting would inherit that corruption.
    """
    print(f"  {label:<18}", end="", flush=True)
    start = time.monotonic()
    deadline = start + seconds
    if inject:
        injector, frame, interval = inject
        nxt = start
        while time.monotonic() < deadline:
            injector.send(frame)
            nxt += interval
            time.sleep(max(0.0, nxt - time.monotonic()))
    else:
        time.sleep(seconds)
    end = time.monotonic()
    time.sleep(0.05)
    raw = monitor.window(start, end)
    hits = sum(raw.count(bytes([REINIT_OPCODE, 0x00, b])) for b in (0x00, 0x01))
    print(f"rx {len(raw):6d} B   re-initialisations {hits:3d}")
    return hits


def cmd_state(args: argparse.Namespace, monitor: BusMonitor, injector: Injector) -> int:
    """Report every dimmer channel MC2E believes is on.

    MC2E only says this while re-initialising a module, so there is nothing to
    read on a quiet bus. `--nudge` provokes it by writing inert polls of a
    vacant address; without it this just watches and will usually see nothing.
    """
    print(f"listening {args.seconds:.0f}s" + (" with nudging" if args.nudge else "") + "\n")
    start = time.monotonic()
    deadline = start + args.seconds
    nxt = start
    while time.monotonic() < deadline:
        if args.nudge:
            injector.send(bytes([CONTROL_ADDR, 0x00]))
        nxt += 2.0
        time.sleep(max(0.0, min(nxt, deadline) - time.monotonic()))
    end = time.monotonic()
    time.sleep(0.05)

    seen: dict[int, list[tuple[int, int]]] = {}
    for frame in parse(monitor.window(start, end)):
        if frame.kind != "data":
            continue
        channels = decode_channel_map(frame.payload)
        if channels is not None:
            seen[frame.addr] = channels
    if not seen:
        print("  nothing reported. MC2E only announces module state while recovering;")
        print("  re-run with --nudge, or accept that a quiet bus stays quiet.")
        return 1
    for addr in sorted(seen):
        levels = "  ".join(f"ch{c}={'ON ' if v else 'off'}" for c, v in seen[addr])
        print(f"  module {addr:02X}   {levels}")
    return 0


def cmd_replay(args: argparse.Namespace, monitor: BusMonitor, injector: Injector) -> int:
    if args.command == "on":
        frames = (POOL_BATH_ON,) + (LEDS_ON if args.with_leds else ())
    else:
        frames = (POOL_BATH_OFF,) + (LEDS_OFF if args.with_leds else ())
    return _blast(args, monitor, injector, frames, f"Pool Bath {args.command}")


def cmd_send(args: argparse.Namespace, monitor: BusMonitor, injector: Injector) -> int:
    frames = tuple(bytes.fromhex(h.replace(" ", "")) for h in args.hex)
    return _blast(args, monitor, injector, frames, "custom")


def _blast(
    args: argparse.Namespace,
    monitor: BusMonitor,
    injector: Injector,
    frames: tuple[bytes, ...],
    what: str,
) -> int:
    print(f"{what}: {args.repeat} passes, direction mode {args.rts_mode}")
    for f in frames:
        print(f"  {f.hex(' ').upper()}")
    if args.dry_run:
        print("\ndry run, nothing written")
        return 0
    print(
        "\n  Expect this not to hold. Writing from this adapter blocks the wire long\n"
        "  enough that MC2E loses a module and re-establishes it, and its recovery\n"
        "  re-asserts every channel from its own state - which says this light is\n"
        "  off. Anything that lands gets put back within seconds. Keep --repeat low:\n"
        "  more passes mean more disruption, not a better chance of sticking.\n"
    )
    start = time.monotonic()
    for _ in range(args.repeat):
        for f in frames:
            injector.send(f)
        time.sleep(args.interval)
    end = time.monotonic()
    time.sleep(0.05)
    stats = summarise(monitor.window(start, end), end - start)
    print(f"  wrote {injector.frames_written} frames, {injector.bytes_written} bytes")
    print(f"  bus during: {stats.line()}")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", help="serial device (default: the only /dev/cu.usbserial-*)")
    p.add_argument("--raw-log", type=Path, help="append the raw byte stream here as JSONL")
    p.add_argument(
        "--rts-mode",
        default="none",
        choices=(*Injector.MODES, "auto"),
        help="direction control (default none = assume TXDEN; auto = try all, selftest only)",
    )
    p.add_argument("--interval", type=float, default=0.05, help="seconds between passes")
    sub = p.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="is the tap good enough to trust? run this first")
    health.add_argument("--seconds", type=float, default=15.0)
    health.add_argument(
        "--wait", type=float, default=0.0, help="keep resampling this long for a healthy window"
    )

    listen = sub.add_parser("listen", help="watch the bus and report on it")
    listen.add_argument("--seconds", type=float, default=20.0)

    st = sub.add_parser("selftest", help="does this adapter transmit? positive/negative control")
    st.add_argument(
        "--window",
        type=float,
        default=45.0,
        help="seconds per observation window; short windows let the recovery tail dominate",
    )
    st.add_argument(
        "--control",
        type=lambda s: int(s, 0),
        default=CONTROL_ADDR,
        help="vacant address to poll as the stimulus (inert if it lands)",
    )

    state = sub.add_parser("state", help="what MC2E believes every dimmer channel is doing")
    state.add_argument("--seconds", type=float, default=50.0)
    state.add_argument(
        "--nudge",
        action="store_true",
        help="provoke MC2E into reporting by writing inert polls; disturbs the bus",
    )

    for name, helptext in (("on", "turn the Pool Bath light on"), ("off", "turn it off")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument(
            "--repeat", type=int, default=6, help="passes; low on purpose, see the warning"
        )
        c.add_argument("--with-leds", action="store_true", help="also repaint the Good Bye LEDs")
        c.add_argument("--dry-run", action="store_true")

    send = sub.add_parser("send", help="write arbitrary frames, hex, no length prefix added")
    send.add_argument("hex", nargs="+")
    send.add_argument("--repeat", type=int, default=6)
    send.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.rts_mode == "auto" and args.command != "selftest":
        sys.exit("--rts-mode auto only makes sense for selftest")

    path = resolve_port(args.port)
    port = open_bus(path)
    print(f"port {path} @ {BAUD} 8N1\n")
    monitor = BusMonitor(port, args.raw_log)
    monitor.start()
    injector = Injector(port, "none" if args.rts_mode == "auto" else args.rts_mode)
    handler = {
        "health": cmd_health,
        "listen": cmd_listen,
        "state": cmd_state,
        "selftest": cmd_selftest,
        "on": cmd_replay,
        "off": cmd_replay,
        "send": cmd_send,
    }[args.command]
    try:
        return handler(args, monitor, injector)
    finally:
        monitor.stop()
        port.close()


if __name__ == "__main__":
    raise SystemExit(main())
