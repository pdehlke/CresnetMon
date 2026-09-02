"""Asyncio CIP client for a Crestron control processor.

The wire format is the one proven by mac/cip_xpanel.py and mac/poc_joinpress.py
in this repo, re-implemented on asyncio because the proof-of-concept client is
blocking and Home Assistant cannot host a blocking socket loop.

Packet framing is `<type> <len hi> <len lo> <payload>`.

The digital-join encoding is identical in both directions: datatype 0x00, then
the 0-based join low byte, then a byte whose top bit is SET for low and CLEAR for
high. Pressing join 24 is `05 00 06 00 00 03 00 17 00` and releasing it is
`05 00 06 00 00 03 00 17 80`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

HEARTBEAT = b"\x0d\x00\x02\x00\x00"
UPDATE_REQUEST = b"\x05\x00\x05\x00\x00\x02\x03\x00"
END_OF_QUERY_ACK = b"\x05\x00\x05\x00\x00\x02\x03\x1d"

HEARTBEAT_INTERVAL = 15.0
# The AADS tolerated 149s of total silence from a registered client without
# dropping it (measured 2026-09-02), so this interval is comfortable rather than
# marginal. Registration plus a full state dump takes about 1.1s, which is why
# reconnecting is cheap enough to do eagerly.
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
SYNC_QUIET_SECONDS = 2.0
PRESS_HOLD_SECONDS = 0.12


def registration(ipid: int) -> bytes:
    """The registration packet for one IP-ID."""
    return b"\x01\x00\x0b\x00\x00\x00\x00\x00" + bytes([ipid]) + b"\x40\xff\xff\xf1\x01"


def digital_packet(join: int, pressed: bool) -> bytes:
    """Encode a digital join press or release.

    `join` is 1-based, as everything in the documented join maps is; the wire is
    0-based, hence the -1.
    """
    n = join - 1
    body = bytes([0x00, n & 0xFF, ((n >> 8) & 0x7F) | (0x00 if pressed else 0x80)])
    payload = bytes([0x00, 0x00, len(body)]) + body
    return bytes([0x05, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


def decode_digitals(body: bytes) -> list[tuple[int, int]]:
    """Decode a digital-join data body into (join, value) pairs.

    One packet can carry several joins: the AADS packs five into a single frame,
    and reading only the first loses the rest.
    """
    out: list[tuple[int, int]] = []
    for i in range(0, len(body) - 1, 2):
        join = (((body[i + 1] & 0x7F) << 8) | body[i]) + 1
        out.append((join, ((body[i + 1] & 0x80) >> 7) ^ 1))
    return out


class CipClient:
    """Holds one registered CIP session, reconnecting for as long as it is running."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        ipid: int,
        on_digital: Callable[[int, int], None],
        on_state: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.ipid = ipid
        self._on_digital = on_digital
        self._on_state = on_state

        self.digital: dict[int, int] = {}
        self.analog: dict[int, int] = {}
        self.serial: dict[int, str] = {}

        self.connected = False
        self.synced = False

        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_rx = 0.0
        self._write_lock = asyncio.Lock()

    # ---- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"crestron_cip-{self.name}")

    async def async_stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close()

    async def _close(self) -> None:
        writer, self._writer = self._writer, None
        self.connected = False
        self.synced = False
        if writer:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    # ---- connection loop ---------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.IncompleteReadError) as err:
                _LOGGER.warning("%s: connection lost (%s)", self.name, err)
            except Exception:
                _LOGGER.exception("%s: unexpected failure in CIP session", self.name)
            finally:
                await self._close()
                if self._on_state:
                    self._on_state()

            if not self._running:
                return
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            _LOGGER.debug("%s: reconnecting in %.0fs", self.name, delay)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer
        self.connected = True
        self.synced = False
        # State from a previous session is not evidence about this one. Anything
        # that changed while we were away arrives in the new dump, and anything
        # that did not is re-asserted by it.
        self.digital.clear()
        _LOGGER.info("%s: connected to %s:%d", self.name, self.host, self.port)

        loop = asyncio.get_running_loop()
        self._last_rx = loop.time()
        last_beat = loop.time()
        buf = b""

        while self._running:
            try:
                chunk = await asyncio.wait_for(reader.read(8192), timeout=1.0)
            except TimeoutError:
                chunk = b""
            if chunk == b"" and reader.at_eof():
                raise OSError("processor closed the connection")
            if chunk:
                self._last_rx = loop.time()
                buf += chunk

            while len(buf) >= 3:
                length = (buf[1] << 8) + buf[2]
                if len(buf) < length + 3:
                    break
                await self._handle(buf[0], buf[3 : 3 + length])
                buf = buf[length + 3 :]

            # The processor marks the end of its dump explicitly, but a slot with
            # nothing to say would never send it, so quiet also counts as synced.
            quiet_for = loop.time() - self._last_rx
            if self.connected and not self.synced and quiet_for > SYNC_QUIET_SECONDS:
                self._mark_synced()

            now = loop.time()
            if now - last_beat >= HEARTBEAT_INTERVAL:
                await self._send(HEARTBEAT)
                last_beat = now

    def _mark_synced(self) -> None:
        if self.synced:
            return
        self.synced = True
        _LOGGER.info(
            "%s: synced, %d digital joins reported (%d high), %d analog, %d serial",
            self.name,
            len(self.digital),
            sum(1 for v in self.digital.values() if v),
            len(self.analog),
            len(self.serial),
        )
        if self._on_state:
            self._on_state()

    # ---- protocol ----------------------------------------------------------

    async def _handle(self, ciptype: int, payload: bytes) -> None:
        if ciptype == 0x0F:
            await self._send(registration(self.ipid))
        elif ciptype == 0x02:
            # klenae's reference client accepts only status 0x1f; this 2009
            # firmware answers 0x03, so accept any 00 00 00 xx and log the code.
            if len(payload) == 4 and payload[:3] == b"\x00\x00\x00":
                _LOGGER.info(
                    "%s: registered as IP-ID 0x%02X (status 0x%02x)",
                    self.name,
                    self.ipid,
                    payload[3],
                )
                await self._send(UPDATE_REQUEST)
            elif payload == b"\xff\xff\x02":
                raise OSError(f"IP-ID 0x{self.ipid:02X} does not exist on {self.host}")
            else:
                raise OSError(f"registration refused: {payload.hex(' ')}")
        elif ciptype == 0x03:
            raise OSError("processor disconnected us")
        elif ciptype == 0x05:
            await self._handle_data(payload)
        elif ciptype == 0x12:
            join = ((payload[5] << 8) | payload[6]) + 1
            self.serial[join] = payload[8:].decode("latin-1")

    async def _handle_data(self, payload: bytes) -> None:
        datatype = payload[3]
        body = payload[4:]
        if datatype == 0x00:
            for join, value in decode_digitals(body):
                previous = self.digital.get(join)
                self.digital[join] = value
                if previous != value:
                    self._on_digital(join, value)
        elif datatype == 0x14:
            for i in range(0, len(body) - 3, 4):
                join = ((body[i] << 8) | body[i + 1]) + 1
                self.analog[join] = (body[i + 2] << 8) | body[i + 3]
        elif datatype == 0x01:
            self.analog[body[0] + 1] = (body[1] << 8) | body[2]
        elif datatype == 0x15:
            # The join is 0-based on the wire like every other type here; this
            # branch was the one place that once missed the +1.
            self.serial[((body[0] << 8) | body[1]) + 1] = body[3:].decode("latin-1")
        elif datatype == 0x02:
            text = body.decode("latin-1")
            head = text.split(",", 1)[0].lstrip("#")
            self.serial[int(head) if head.isdigit() else 0] = text
        elif datatype == 0x03 and body and body[0] == 0x1C:
            await self._send(END_OF_QUERY_ACK)
            await self._send(HEARTBEAT)
            self._mark_synced()

    # ---- writing -----------------------------------------------------------

    async def _send(self, packet: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise OSError(f"{self.name}: not connected")
        async with self._write_lock:
            writer.write(packet)
            await writer.drain()

    async def async_press(self, join: int, hold: float = PRESS_HOLD_SECONDS) -> None:
        """Tap a digital join: press, hold, release."""
        await self._send(digital_packet(join, True))
        await asyncio.sleep(hold)
        await self._send(digital_packet(join, False))
