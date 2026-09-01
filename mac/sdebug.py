"""Capture MC2E SDEBUG output scoped to one Ethernet ID, then always turn it off.

Read-only with respect to the house: SDEBUG only controls console printing.
The teardown runs in a finally block so an exception or a timeout still leaves
the processor's debug flags off.
"""

import argparse
import socket
import sys
import time

import crestron_console as ccon

ON = [
    "SDEBUG -DON {target}",
    "SDEBUG -RXION",
    "SDEBUG -TXION",
    "SDEBUG -RXF1",
    "SDEBUG -TXF1",
    "SDEBUG -STON",
]
OFF = [
    "SDEBUG -DOFF {target}",
    "SDEBUG -RXIOFF",
    "SDEBUG -TXIOFF",
    "SDEBUG -RXROFF",
    "SDEBUG -TXROFF",
    "SDEBUG -STOFF",
]


def send(sock, cmd, settle=1.5):
    sock.sendall(cmd.encode() + b"\r\n")
    return ccon.drain(sock, settle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.4.59")
    ap.add_argument("--target", default="E05")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--out", default="sdebug-capture.txt")
    args = ap.parse_args()

    sock = socket.create_connection((args.host, 23), timeout=8)
    ccon.drain(sock, 3.0)
    captured = ""
    try:
        print("=== settings before ===")
        print(send(sock, "SDEBUG -S1", 3.0))
        for cmd in ON:
            send(sock, cmd.format(target=args.target))
        print("=== settings after enabling ===")
        print(send(sock, "SDEBUG -S1", 3.0))

        print(f"=== capturing {args.seconds:.0f}s ===", flush=True)
        sock.settimeout(0.5)
        chunks, deadline = [], time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            try:
                data = sock.recv(8192)
            except TimeoutError:
                continue
            if not data:
                break
            chunks.append(ccon.strip_iac(sock, data, None))
        captured = b"".join(chunks).decode("latin-1")
    finally:
        try:
            sock.settimeout(5.0)
            for cmd in OFF:
                send(sock, cmd.format(target=args.target), 1.0)
            print("=== settings after teardown ===")
            print(send(sock, "SDEBUG -S1", 3.0))
        except OSError as exc:
            print(f"!! teardown failed, disable SDEBUG by hand: {exc}", file=sys.stderr)
        finally:
            sock.close()

    with open(args.out, "w") as fh:
        fh.write(captured)
    print(f"captured {len(captured)} bytes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
