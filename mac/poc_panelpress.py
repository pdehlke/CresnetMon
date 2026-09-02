"""Press one digital join on a freed TSW-752 slot and watch what the AADS reports.

No Cresnet tap, no SDEBUG: this only registers over CIP, presses, and listens.
Joins that the panel project shares with the DSC alarm keypad are refused
outright rather than left to a command-line typo.
"""

import argparse
import socket
import sys
import time

from cip_xpanel import PORT, Listener
from poc_joinpress import HOLD_SECONDS, digital, pump

# 5-SEC (ALARM-DSC-pg01-main) uses d130-d141 and d146-d148; d93 enters the
# alarm subsystem from the home page. The Kitchen lighting block sits inside
# that range, which is why it is refused too: use MC2E IP-ID 0x03 for Kitchen.
FORBIDDEN = set(range(130, 149)) | {93}

ap = argparse.ArgumentParser()
ap.add_argument("--join", type=int, required=True)
ap.add_argument("--host", default="192.168.4.61")
ap.add_argument("--ipid", type=lambda s: int(s, 0), default=0x13)
ap.add_argument("--presses", type=int, default=1)
ap.add_argument("--watch", type=float, default=12.0)
ap.add_argument("--prefix", type=int, default=0, help="press this join first, e.g. 91 for Lights")
a = ap.parse_args()

for j in (a.join, a.prefix):
    if j in FORBIDDEN:
        sys.exit(f"refusing join {j}: shared with the DSC alarm keypad")

listener = Listener(a.ipid, False)
sock = socket.create_connection((a.host, PORT), timeout=5)
sock.settimeout(0.5)
listener.log(f"connected to {a.host}:{PORT}")
pump(sock, listener, 6.0)
if not listener.synced:
    sys.exit("never reached end of state dump; aborting without pressing")


def tap_join(j):
    listener.log(f"PRESS d{j}  {digital(j, True).hex(' ')}")
    sock.sendall(digital(j, True))
    time.sleep(HOLD_SECONDS)
    sock.sendall(digital(j, False))


if a.prefix:
    tap_join(a.prefix)
    pump(sock, listener, 2.0)

for _ in range(a.presses):
    tap_join(a.join)
    pump(sock, listener, a.watch)

listener.log(f"{len(listener.changes)} changes after sync")
for t, kind, join, value in listener.changes:
    print(f"  {t:7.3f}  {kind}{join} = {value!r}")
sock.close()
