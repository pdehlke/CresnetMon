"""Minimal Crestron console (CTP/telnet) client.

Refuses telnet option negotiation rather than implementing it, sends each
argument as one command line, and prints everything that comes back. Used
against the MC2E (192.168.4.59), the AADS (192.168.4.61) and the TSW-752 panels
(port 23), none of which ask for a password.

Read-only by intent: nothing here sends a command that changes processor or
house state. `sdebug.py` is the one caller that does write, and it confines
itself to SDEBUG print flags with a guaranteed teardown.

    uv run python crestron_console.py VER IPTABLE WHO
"""

import socket
import sys
import time

HOST, PORT = "192.168.4.59", 23  # MC2E; override by assigning ccon.HOST
IAC, DONT, WONT, DO, WILL, SB, SE = 255, 254, 252, 253, 251, 250, 240


def strip_iac(sock, data, buf):
    """Answer option negotiation with a flat refusal, return the plain bytes."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= len(data):
            break
        cmd = data[i + 1]
        if cmd in (DO, DONT, WILL, WONT):
            if i + 2 >= len(data):
                break
            opt = data[i + 2]
            reply = WONT if cmd == DO else (DONT if cmd == WILL else None)
            if reply:
                sock.sendall(bytes([IAC, reply, opt]))
            i += 3
        elif cmd == SB:
            j = data.find(bytes([IAC, SE]), i)
            i = len(data) if j < 0 else j + 2
        else:
            i += 2
    return bytes(out)


def drain(sock, seconds=2.0):
    sock.settimeout(0.4)
    chunks, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data = sock.recv(4096)
        except TimeoutError:
            if chunks:
                break
            continue
        if not data:
            break
        chunks.append(strip_iac(sock, data, None))
    return b"".join(chunks).decode("latin-1")


def main(cmds, settle=2.0):
    s = socket.create_connection((HOST, PORT), timeout=8)
    print("=== banner ===")
    print(drain(s, 3.0))
    for c in cmds:
        print(f"\n=== {c} ===")
        s.sendall(c.encode() + b"\r\n")
        print(drain(s, settle))
    s.close()


if __name__ == "__main__":
    main(sys.argv[1:])
