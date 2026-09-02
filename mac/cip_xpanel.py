"""Register as a Crestron XPanel over CIP and log every join the processor
reports. Sends nothing but the registration handshake and heartbeats.

Wire format taken from klenae/python-cipclient (MIT), re-implemented here so
the PoC stays self-contained and provably read-only.

Packet: <type> <len hi> <len lo> <payload>
"""

import argparse
import socket
import sys
import time

HOST, PORT, IPID = "192.168.4.59", 41794, 0x03

HEARTBEAT = b"\x0d\x00\x02\x00\x00"
UPDATE_REQUEST = b"\x05\x00\x05\x00\x00\x02\x03\x00"
END_OF_QUERY_ACK = b"\x05\x00\x05\x00\x00\x02\x03\x1d"


def registration(ipid: int) -> bytes:
    return b"\x01\x00\x0b\x00\x00\x00\x00\x00" + bytes([ipid]) + b"\x40\xff\xff\xf1\x01"


class Listener:
    def __init__(self, ipid: int, verbose: bool) -> None:
        self.ipid = ipid
        self.verbose = verbose
        self.digital: dict[int, int] = {}
        self.analog: dict[int, int] = {}
        self.serial: dict[int, str] = {}
        self.registered = False
        self.synced = False
        self.last_rx = time.monotonic()
        self.t0 = time.monotonic()
        self.changes: list[tuple[float, str, int, object]] = []

    def log(self, msg: str) -> None:
        print(f"[{time.monotonic() - self.t0:7.3f}] {msg}", flush=True)

    def record(self, kind: str, join: int, value) -> None:
        table = {"d": self.digital, "a": self.analog, "s": self.serial}[kind]
        previous = table.get(join)
        table[join] = value
        # A join seen for the first time in an inactive state is not news: only
        # high/non-zero joins are dumped, so anything arriving as 0 or "" is a
        # join we simply had no prior value for. Reporting those as changes
        # produced four phantom "changes" before any stimulus in the 0x11 run.
        first_sight_inactive = previous is None and not value
        if self.synced and previous != value and not first_sight_inactive:
            self.changes.append((time.monotonic() - self.t0, kind, join, value))
            self.log(f"CHANGE {kind}{join} = {value!r} (was {previous!r})")
        elif self.verbose:
            self.log(f"  {kind}{join} = {value!r}")

    def handle(self, ciptype: int, payload: bytes, out: list[bytes]) -> None:
        self.last_rx = time.monotonic()
        if ciptype == 0x0F:
            self.log("processor asked us to register")
            out.append(registration(self.ipid))
        elif ciptype == 0x02:
            # klenae's client only accepts status 0x1f; this 2009 firmware
            # answers 0x03. Accept any 00 00 00 xx and report the code.
            if len(payload) == 4 and payload[:3] == b"\x00\x00\x00":
                self.registered = True
                self.log(f"registered as IP-ID 0x{self.ipid:02X} (status 0x{payload[3]:02x})")
                out.append(UPDATE_REQUEST)
            elif payload == b"\xff\xff\x02":
                sys.exit(f"IP-ID 0x{self.ipid:02X} does not exist on this processor")
            else:
                sys.exit(f"registration refused: {payload.hex(' ')}")
        elif ciptype == 0x03:
            sys.exit("processor disconnected us")
        elif ciptype in (0x0D, 0x0E):
            pass
        elif ciptype == 0x05:
            datatype = payload[3]
            body = payload[4:]
            if datatype == 0x00:
                # Digital. One or more 2-byte entries per packet: the AADS packs
                # five into one frame, and reading only the first loses the rest.
                for i in range(0, len(body) - 1, 2):
                    join = (((body[i + 1] & 0x7F) << 8) | body[i]) + 1
                    self.record("d", join, ((body[i + 1] & 0x80) >> 7) ^ 1)
            elif datatype == 0x14:
                # Analog, also packed: 2-byte join then 2-byte value, repeating.
                for i in range(0, len(body) - 3, 4):
                    join = (body[i] << 8) | body[i + 1]
                    self.record("a", join + 1, (body[i + 2] << 8) | body[i + 3])
            elif datatype == 0x01:
                # Compact analog the 2009 MC2E uses: 1-byte join, 2-byte value.
                self.record("a", body[0] + 1, (body[1] << 8) | body[2])
            elif datatype == 0x15:
                # Serial with a 2-byte join and one flag byte before the text.
                # This is the form that carries the AADS's menu labels. The join
                # is 0-based on the wire like every other type here, and this
                # branch was the one place missing the +1: it reported the
                # TSW-752 panel project's serials one low across the board.
                self.record("s", ((body[0] << 8) | body[1]) + 1, body[3:].decode("latin-1"))
            elif datatype == 0x02:
                # Serial the 2009 MC2E uses, self-labelling as "#<join>,<text>".
                text = body.decode("latin-1")
                head = text.split(",", 1)[0].lstrip("#")
                self.record("s", int(head) if head.isdigit() else 0, text)
            elif datatype == 0x20:
                printable = "".join(chr(c) if 32 <= c < 127 else "." for c in body)
                self.log(f"panel identity: {printable}")
            elif datatype == 0x03:
                kind = body[0]
                if kind == 0x1C:
                    self.log("end of state dump")
                    out.append(END_OF_QUERY_ACK)
                    out.append(HEARTBEAT)
                    self.synced = True
                    self.summary()
            elif datatype == 0x08:
                self.log(f"processor clock: {body.hex(' ')}")
            else:
                self.log(f"unhandled datatype 0x{datatype:02x}: {body.hex(' ')}")
        elif ciptype == 0x12:
            join = ((payload[5] << 8) | payload[6]) + 1
            self.record("s", join, payload[8:].decode("latin-1"))

    def summary(self) -> None:
        high = sorted(j for j, v in self.digital.items() if v)
        nonzero = sorted(j for j, v in self.analog.items() if v)
        labelled = sorted(j for j, v in self.serial.items() if v.strip())
        self.log(
            f"state: {len(self.digital)} digital joins reported, {len(high)} high; "
            f"{len(self.analog)} analog, {len(nonzero)} non-zero; {len(self.serial)} serial"
        )
        if high:
            self.log(f"  digital high: {high}")
        if nonzero:
            self.log(f"  analog non-zero: {[(j, self.analog[j]) for j in nonzero]}")
        for j in labelled:
            self.log(f"  serial {j}: {self.serial[j]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--ipid", type=lambda s: int(s, 0), default=IPID)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument(
        "--repoll", type=float, default=0.0, help="re-send the update request every N seconds"
    )
    args = ap.parse_args()

    listener = Listener(args.ipid, args.verbose)
    sock = socket.create_connection((args.host, PORT), timeout=5)
    sock.settimeout(0.5)
    listener.log(f"connected to {args.host}:{PORT}")

    buf = b""
    deadline = time.monotonic() + args.seconds
    last_beat = time.monotonic()
    last_poll = time.monotonic()
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            out: list[bytes] = []
            try:
                data = sock.recv(8192)
                if not data:
                    listener.log("connection closed by processor")
                    break
                buf += data
            except TimeoutError:
                pass
            while len(buf) >= 3:
                payload_length = (buf[1] << 8) + buf[2]
                if len(buf) < payload_length + 3:
                    break
                if args.raw:
                    listener.log(f"RX type 0x{buf[0]:02x} <{buf[3 : 3 + payload_length].hex(' ')}>")
                listener.handle(buf[0], buf[3 : 3 + payload_length], out)
                buf = buf[payload_length + 3 :]
            if not listener.synced and listener.registered and now - listener.last_rx > 2.0:
                listener.synced = True
                listener.log("state dump quiet for 2s, watching for changes")
                listener.summary()
            if args.repoll and time.monotonic() - last_poll >= args.repoll:
                listener.log("re-sending update request")
                out.append(UPDATE_REQUEST)
                last_poll = time.monotonic()
            if time.monotonic() - last_beat >= 15:
                out.append(HEARTBEAT)
                last_beat = time.monotonic()
            for packet in out:
                sock.sendall(packet)
    finally:
        sock.close()

    print()
    listener.log(f"{len(listener.changes)} changes seen after sync")
    for t, kind, join, value in listener.changes:
        print(f"  {t:7.3f}  {kind}{join} = {value!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
