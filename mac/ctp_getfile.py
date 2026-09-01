"""Retrieve a file from a Crestron processor over the CTP console via XMODEM.

The telnet console on port 23 refuses this: `XGETFILE` there answers "Command
Blocked from this console type". The CTP console on port 41795 is the same
plain-text console with no telnet option negotiation, which means it both
permits `XGETFILE` and passes binary through unescaped (no IAC doubling to
undo). That is the whole reason this talks to 41795 rather than reusing
`crestron_console.py`.

Used to pull `Gale Favela 11-14-08.bin` off the MC2E so the XPanel join map can
be read out of the compiled program by name, rather than discovered by pressing
unknown joins on an interface that may also reach the alarm.

    uv run python ctp_getfile.py "Gale Favela 11-14-08.rte" gale.rte
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

HOST, CTP_PORT = "192.168.4.59", 41795

SOH, STX, EOT, ACK, NAK, CAN, SUB = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, 0x1A


def crc16(data: bytes) -> int:
    """XMODEM's CRC-16/XMODEM: polynomial 0x1021, zero seed, no reflection."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Reader:
    """Socket reader with pushback, so a packet can be pulled out byte-exactly."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def read(self, count: int, timeout: float) -> bytes:
        self.sock.settimeout(timeout)
        while len(self.buf) < count:
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            self.buf += chunk
        out, self.buf = self.buf[:count], self.buf[count:]
        return out


def receive(sock: socket.socket, reader: Reader, verbose: bool) -> bytes:
    """Standard XMODEM receiver, handling both 128B (SOH) and 1K (STX) blocks."""
    data = bytearray()
    expect = 1
    crc_mode = True
    started = False
    idle = 0

    sock.sendall(b"C")  # 'C' requests CRC-16; NAK would request plain checksum
    while idle < 40:
        head = reader.read(1, 3.0)
        if not head:
            idle += 1
            if not started:
                # Alternate the two handshake bytes until the sender answers.
                crc_mode = not crc_mode
                sock.sendall(b"C" if crc_mode else bytes([NAK]))
            continue
        marker = head[0]

        if marker == EOT:
            sock.sendall(bytes([ACK]))
            if verbose:
                print(f"  EOT after {len(data)} bytes")
            return bytes(data)
        if marker == CAN:
            print("  sender cancelled", file=sys.stderr)
            return bytes(data)
        if marker not in (SOH, STX):
            continue  # console banner or padding ahead of the first packet

        size = 128 if marker == SOH else 1024
        trail_len = 2 if crc_mode else 1
        started, idle = True, 0
        body = reader.read(2 + size + trail_len, 6.0)
        if len(body) < 2 + size + trail_len:
            sock.sendall(bytes([NAK]))
            continue

        blk, nblk = body[0], body[1]
        payload = body[2 : 2 + size]
        trail = body[2 + size :]

        good = (blk ^ 0xFF) == nblk
        if good:
            good = (
                crc16(payload) == (trail[0] << 8 | trail[1])
                if crc_mode
                else (sum(payload) & 0xFF) == trail[0]
            )
        if not good:
            sock.sendall(bytes([NAK]))
            continue

        if blk == expect:  # a repeated block means our ACK was lost; re-ACK only
            data += payload
            expect = (expect + 1) % 256
            if verbose and expect % 32 == 0:
                print(f"  {len(data)} bytes", flush=True)
        sock.sendall(bytes([ACK]))

    print("  timed out waiting for the sender", file=sys.stderr)
    return bytes(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name", help="file on the processor, e.g. 'Gale Favela 11-14-08.bin'")
    ap.add_argument("out", help="local path to write")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=CTP_PORT)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    sock = socket.create_connection((args.host, args.port), timeout=8)
    reader = Reader(sock)
    reader.read(4096, 2.0)  # console banner

    # Unquoted: quoting the name gets "File(s) ... Not Found", spaces and all.
    sock.sendall(f"XGETFILE {args.name}\r\n".encode())
    prompt = reader.read(120, 3.0)
    if not args.quiet:
        print(f"prompt: {prompt[:100]!r}")

    started = time.monotonic()
    data = receive(sock, reader, not args.quiet)
    sock.close()

    # XMODEM pads the final block to a fixed size with SUB (0x1A).
    data = data.rstrip(bytes([SUB]))
    with open(args.out, "wb") as fh:
        fh.write(data)
    print(f"wrote {args.out}: {len(data)} bytes in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
