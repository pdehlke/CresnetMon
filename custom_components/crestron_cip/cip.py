"""Asyncio CIP client for a Crestron control processor.

The wire format is the one proven by mac/cip_xpanel.py and mac/poc_joinpress.py
in this repo, re-implemented on asyncio because the proof-of-concept client is
blocking and Home Assistant cannot host a blocking socket loop.

Packet framing is `<type> <len hi> <len lo> <payload>`.

The digital-join encoding is identical in both directions: datatype 0x00, then
the 0-based join low byte, then a byte whose top bit is SET for low and CLEAR for
high. Pressing join 24 is `05 00 06 00 00 03 00 17 00` and releasing it is
`05 00 06 00 00 03 00 17 80`.

A slot on the AADS holds exactly one subsystem at a time, entered by pressing a
join on the panel's home page, and the join space is reused across subsystems:
`d101` is Dining Room Table inside Lights and the AppleTV menu inside A/V. So the
subsystem is state this client has to track rather than a thing it enters once,
joins are bucketed by the subsystem that produced them, and nothing is written
without naming the subsystem it belongs to. See the pdehlke/homeassistant repo,
docs/crestron/crestron-subsystem-time-slicing.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .const import (
    BRINGUP_RETRY_SECONDS,
    ENTRY_MIN_SECONDS,
    ENTRY_QUIET_SECONDS,
    ENTRY_TIMEOUT,
    REPOLL_TIMEOUT,
)

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
ENTRY_POLL_SECONDS = 0.02


class CrestronError(Exception):
    """Raised when a command cannot be carried out safely or at all.

    Defined here rather than in bridge.py because the last check before bytes
    reach the wire lives in this module, and bridge.py imports this one.
    """


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
        on_digital: Callable[[int, int, str | None], None],
        on_state: Callable[[], None] | None = None,
        subsystems: dict[str, int] | None = None,
        default_subsystem: str | None = None,
        forbidden: frozenset[int] = frozenset(),
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.ipid = ipid
        # Empty on an ungated link. The MC2E XPanel slot has no subsystems at
        # all, which is why the Kitchen kept working through the 2026-09-15
        # outage, and why every path below has to be a no-op there.
        self.subsystems = dict(subsystems or {})
        self.default_subsystem = default_subsystem
        self.forbidden = frozenset(forbidden)
        self._on_digital = on_digital
        self._on_state = on_state

        # Which subsystem the slot is in. None means unknown, which is both what
        # a fresh session starts as and what a failed entry leaves behind, and
        # nothing may be written while it holds.
        self.current_subsystem: str | None = None

        # Joins bucketed by the subsystem that reported them. The None bucket is
        # the pre-entry menu on a gated link and everything on an ungated one.
        self._digital: dict[str | None, dict[int, int]] = {}
        self._analog: dict[str | None, dict[int, int]] = {}
        self.serial: dict[int, str] = {}

        self.connected = False
        self.synced = False
        # Bumped on every new session. The A/V cursor is per slot and no
        # physical panel can move ours, so a cached cursor position stays
        # true for as long as one session lives and for no longer.
        self.generation = 0
        # Counts analog frames received, not values changed. A cursor move
        # that lands on a zone holding the same volume as the last one sends
        # a frame carrying an identical value, and 'the processor has spoken
        # since the press' is the only question worth asking.
        self.analog_rx = 0

        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._bringup: asyncio.Task | None = None
        self._running = False
        self._last_rx = 0.0
        self._last_data_rx = 0.0
        self._last_bringup = 0.0
        self._collecting: dict[int, int] | None = None
        self._collecting_for: str | None = None
        self._end_of_query = asyncio.Event()
        self._write_lock = asyncio.Lock()
        # There is one collection buffer, so there is one collection at a
        # time. _bring_up() enters without holding the bridge's per-link
        # lock, so that lock cannot be what serialises entries against it;
        # this is. Without it a bring-up and a command retry racing after a
        # mid-command reconnect overwrite each other's buffer, and the loser
        # spins to ENTRY_TIMEOUT reporting no joins at all.
        self._collect_lock = asyncio.Lock()

    # ---- state -------------------------------------------------------------

    @property
    def digital(self) -> dict[int, int]:
        """The bucket the load table lives in.

        Loads are addressed in the link's default subsystem by definition, so
        this is what `is_on()` reads and what a lighting alias is mirrored onto.
        A/V joins land in another bucket and can never be mistaken for a load,
        which is the whole point of bucketing them.
        """
        return self.digital_for(self.default_subsystem)

    @property
    def analog(self) -> dict[int, int]:
        return self.analog_for(self.default_subsystem)

    def digital_for(self, subsystem: str | None) -> dict[int, int]:
        return self._digital.setdefault(subsystem, {})

    @property
    def _incoming(self) -> str | None:
        """Which subsystem the frames arriving right now belong to.

        During an entry `current_subsystem` is deliberately None, because nothing
        may be written while the slot is in flight. The dump arriving in that
        window still belongs to the subsystem being entered, though, so analog
        joins land in the right bucket rather than in the unknown one. `a11`, the
        per-zone volume, arrives exactly there.
        """
        return self._collecting_for if self._collecting is not None else self.current_subsystem

    def analog_for(self, subsystem: str | None) -> dict[int, int]:
        return self._analog.setdefault(subsystem, {})

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
        self.current_subsystem = None
        self._collecting = None
        self._collecting_for = None
        bringup, self._bringup = self._bringup, None
        if bringup:
            bringup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bringup
        if writer:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    # ---- connection loop ---------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            # Captured in the finally because _close() clears it, and read after
            # it because the escalation is about the next attempt, not this one.
            # This used to be `attempt = 0` on a clean return from _session(),
            # which could never fire: _session()'s only exit that is not its own
            # `while self._running` test going false is a raise, so a clean
            # return meant shutdown and the next statement returned. The
            # backoff therefore climbed for the life of the process and pinned
            # at the last step, however healthy the sessions in between were.
            reached_sync = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.IncompleteReadError) as err:
                _LOGGER.warning("%s: connection lost (%s)", self.name, err)
            except Exception:
                _LOGGER.exception("%s: unexpected failure in CIP session", self.name)
            finally:
                reached_sync = self.synced
                await self._close()
                if self._on_state:
                    self._on_state()

            if not self._running:
                return
            # Syncing is a high bar and cannot be cleared by a flapping link: it
            # takes a registration dump, a quiet window, an entry press and that
            # subsystem's own dump, several seconds of two-way traffic. A link
            # that got that far and then dropped is not the same thing as a
            # processor refusing us, so it starts again from the top.
            if reached_sync:
                attempt = 0
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            _LOGGER.debug("%s: reconnecting in %.0fs", self.name, delay)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer
        self.connected = True
        self.synced = False
        self.generation += 1
        self.current_subsystem = None
        self._collecting = None
        self._collecting_for = None
        # State from a previous session is not evidence about this one. Anything
        # that changed while we were away arrives in the new dump, and anything
        # that did not is re-asserted by it.
        self._digital.clear()
        self._analog.clear()
        _LOGGER.info("%s: connected to %s:%d", self.name, self.host, self.port)

        loop = asyncio.get_running_loop()
        self._last_rx = loop.time()
        self._last_data_rx = loop.time()
        self._last_bringup = 0.0
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

            now = loop.time()
            # The processor marks the end of its registration dump explicitly,
            # but a slot with nothing to say would never send it, so quiet also
            # counts. Either way the bring-up runs as its own task: it waits on
            # traffic that only this loop can receive, so doing it inline would
            # deadlock until the entry timed out.
            if self.connected and not self.synced and now - self._last_rx > SYNC_QUIET_SECONDS:
                self._start_bring_up()

            if now - last_beat >= HEARTBEAT_INTERVAL:
                await self._send(HEARTBEAT)
                last_beat = now

    def _start_bring_up(self) -> None:
        """Enter the default subsystem and call the session synced behind it."""
        if self.synced or self._bringup is not None:
            return
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_bringup < BRINGUP_RETRY_SECONDS:
            return
        self._last_bringup = loop.time()
        self._bringup = asyncio.create_task(
            self._bring_up(), name=f"crestron_cip-bringup-{self.name}"
        )

    async def _bring_up(self) -> None:
        """Finish coming up, entering the gated subsystem first where there is one.

        A registered slot is not yet a useful one on the AADS. The processor
        hands it the menu and then waits for the panel to say which subsystem it
        is showing; until that press arrives it reports no lighting joins and
        acts on none. Reaching `synced` on the strength of the menu dump alone
        would therefore have the bridge report every load off, confidently and
        wrongly, so the entry press happens first and the session is only synced
        once the subsystem's own dump has landed behind that.

        A failed entry leaves `synced` false and returns, so the read loop's next
        quiet period tries again rather than the link sitting dead. That retry is
        rate-limited by BRINGUP_RETRY_SECONDS, because an entry that produced no
        traffic also leaves the link quiet enough to qualify immediately.
        """
        try:
            if not await self.async_enter(self.default_subsystem):
                return
            # The entry dump alone is not a complete picture, so a load that is
            # on but absent from it would read off for the life of the session.
            # This is the one place that matters, because the bucket starts
            # empty here and absence really does mean off.
            #
            # Only on a gated link. An ungated one never enters a subsystem, so
            # its registration dump is already the full one, and the MC2E does
            # not answer a second update request at all: it took the 5s timeout
            # on every startup and then carried on with what it already had.
            if self.subsystems:
                await self.async_repoll(self.default_subsystem)
            self.synced = True
            _LOGGER.info(
                "%s: synced, %d digital joins reported (%d high), %d analog, %d serial",
                self.name,
                len(self.digital),
                sum(1 for v in self.digital.values() if v),
                len(self.analog),
                len(self.serial),
            )
            if self.subsystems and not self.digital:
                # The one failure this link has ever had looks exactly like a
                # quiet house from in here, so say it out loud rather than let
                # thirty loads report off on the strength of an empty dump.
                _LOGGER.warning(
                    "%s: the %s subsystem reported no digital joins at all, so every "
                    "load will read off",
                    self.name,
                    self.default_subsystem,
                )
            if self._on_state:
                self._on_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s: bring-up failed", self.name)
        finally:
            self._bringup = None

    # ---- subsystems --------------------------------------------------------

    async def async_enter(self, subsystem: str | None) -> bool:
        """Put the slot into one subsystem and rebuild that subsystem's state.

        Returns False rather than raising when the processor does not answer, so
        a caller can refuse its own command instead of pressing into a slot whose
        subsystem it does not know.

        The dump is detected by quiet, not by the end-of-query marker the initial
        registration dump ends with. Five live entries on 2026-09-22 produced no
        such marker, and the one that did arrive, on the registration dump, was
        followed by a further 38ms of frames. The thresholds per subsystem are in
        const.py with the measurements behind them.
        """
        if subsystem is None or not self.subsystems:
            return True
        if subsystem not in self.subsystems:
            raise CrestronError(f"{self.name}: no entry join for subsystem {subsystem!r}")
        if self.current_subsystem == subsystem:
            return True

        async with self._collect_lock:
            # Re-checked under the lock: whoever we queued behind may have put
            # the slot exactly where we wanted it, and pressing again would
            # leave the subsystem rather than re-enter it.
            if self.current_subsystem == subsystem:
                return True
            return await self._enter_locked(subsystem)

    async def _enter_locked(self, subsystem: str) -> bool:
        """The entry itself. Call only with `_collect_lock` held."""
        join = self.subsystems[subsystem]
        loop = asyncio.get_running_loop()
        _LOGGER.info(
            "%s: entering the %s subsystem on d%d (was %s)",
            self.name,
            subsystem,
            join,
            self.current_subsystem or "unknown",
        )

        # In flight and therefore unwritable until the dump lands.
        self.current_subsystem = None
        self._collecting = {}
        self._collecting_for = subsystem
        quiet = ENTRY_QUIET_SECONDS.get(subsystem, max(ENTRY_QUIET_SECONDS.values()))
        landed = False
        try:
            self._last_data_rx = loop.time()
            started = loop.time()
            await self._press(join)
            while True:
                await asyncio.sleep(ENTRY_POLL_SECONDS)
                now = loop.time()
                # The floor matters as much as the quiet window. An entry dump
                # arrives in bursts, and on 2026-09-22 a Lights entry on this
                # slot went quiet for longer than the threshold after its first
                # frame, which ended the window 0.47s in on a dump whose last
                # frame lands at about 0.53s. Everything after that arrived
                # outside the collection and moved entities twice.
                if (
                    self._collecting
                    and now - started > ENTRY_MIN_SECONDS
                    and now - self._last_data_rx > quiet
                ):
                    landed = True
                    break
                if now - started > ENTRY_TIMEOUT:
                    _LOGGER.warning(
                        "%s: pressed d%d and the %s subsystem reported %d joins in %.1fs; "
                        "treating the entry as failed",
                        self.name,
                        join,
                        subsystem,
                        len(self._collecting or {}),
                        ENTRY_TIMEOUT,
                    )
                    break
        finally:
            collected, self._collecting = self._collecting or {}, None
            self._collecting_for = None

        if not landed:
            return False
        self._finish_entry(subsystem, collected)
        self.current_subsystem = subsystem
        return True

    async def async_repoll(self, subsystem: str) -> bool:
        """Ask the processor to re-send everything, and merge what comes back.

        An entry dump is partial and not deterministic: on 2026-09-22 one Lights
        entry omitted d241 while that join was high, and three read-only
        registrations in a row saw 23 high joins where a fourth saw 24. The
        update request that follows registration is not like that. Re-sending it
        mid-session returns the full state, about 149 frames in 0.86s measured on
        this processor, and ends with the end-of-query marker, so its completion
        is exact rather than inferred from silence.

        Returns False on timeout rather than raising. The caller still has the
        entry dump, which is incomplete but not wrong.
        """
        async with self._collect_lock:
            return await self._repoll_locked(subsystem)

    async def _repoll_locked(self, subsystem: str) -> bool:
        """The re-poll itself. Call only with `_collect_lock` held."""
        self._end_of_query.clear()
        self._collecting = {}
        self._collecting_for = subsystem
        answered = True
        try:
            await self._send(UPDATE_REQUEST)
            try:
                await asyncio.wait_for(self._end_of_query.wait(), REPOLL_TIMEOUT)
            except TimeoutError:
                _LOGGER.warning(
                    "%s: the update request went unanswered for %.1fs; carrying on with the "
                    "entry dump alone, which may under-report",
                    self.name,
                    REPOLL_TIMEOUT,
                )
                answered = False
        finally:
            collected, self._collecting = self._collecting or {}, None
            self._collecting_for = None

        # Merged even when the marker never came. _handle_data() diverts every
        # digital frame into the buffer while a re-poll is open, so returning
        # early here used to throw away up to REPOLL_TIMEOUT seconds of real
        # feedback and lose a wall-panel press outright. The entry has already
        # succeeded by the time _bring_up() re-polls, so these frames belong to
        # `subsystem` and are not the ambiguous case async_enter() guards
        # against when it discards a failed entry's collection.
        _LOGGER.debug("%s: re-poll returned %d joins", self.name, len(collected))
        self._finish_entry(subsystem, collected)
        return answered

    def invalidate_subsystem(self) -> None:
        """Forget which subsystem the slot is in, forcing the next write to re-enter.

        The AADS is known to clear the latch on a program restart, which also
        drops the session and is handled by reconnecting. Whether anything else
        clears it has never been observed. This is what makes that speculative
        case recoverable instead of permanent: a command that pressed and was
        never confirmed calls this, and the next attempt presses the entry join
        again. See pdehlke/homeassistant issue #25.
        """
        if self.current_subsystem is not None:
            _LOGGER.info(
                "%s: forgetting the %s subsystem, the next write will re-enter",
                self.name,
                self.current_subsystem,
            )
        self.current_subsystem = None

    def _finish_entry(self, subsystem: str, collected: dict[int, int]) -> None:
        """Apply the entry dump to the subsystem's state, reporting what moved.

        A merge, not a rebuild. An earlier version of this treated a join absent
        from the dump as off, on the theory that the dump re-asserts every high
        join and that absence is therefore how an off arrives. Live evidence on
        2026-09-22 killed that theory: entering Lights reported North Sink off
        while the light was physically on, because d241 was not in that entry's
        dump at all and never followed. An entry dump is a statement about the
        joins it contains and says nothing about the ones it omits.

        The cost is the case that motivated the rebuild: a load switched off at a
        wall panel while the slot was away in A/V keeps reading on until some
        later frame corrects it. That is a stale reading. Synthesising an off
        from silence produced a wrong one, which is worse, and reading a lit
        load as off is also the exact signature of the 2026-09-15 outage this
        integration exists to avoid repeating.

        The 2026-09-17 round trip, where a second Lights entry "reported d243
        gone", is consistent with a partial dump rather than with absence
        meaning off, and was over-read at the time.
        """
        previous = self.digital_for(subsystem)
        changed = [
            (join, value) for join, value in collected.items() if previous.get(join) != value
        ]
        previous.update(collected)
        missing = [join for join, value in previous.items() if value and join not in collected]
        if missing:
            # Evidence for the open question of what an entry dump actually
            # covers. These joins were high, the dump did not mention them, and
            # they are deliberately left high rather than assumed off.
            _LOGGER.debug(
                "%s: %s entry dump omitted %d join(s) that were high: %s",
                self.name,
                subsystem,
                len(missing),
                sorted(missing),
            )
        for join, value in changed:
            self._on_digital(join, value, subsystem)

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
        # Tracked separately from _last_rx, which a heartbeat reply also moves.
        # An entry dump is detected by quiet, so counting our own keepalives as
        # traffic would stretch a window that is under a second wide.
        self._last_data_rx = asyncio.get_running_loop().time()
        if datatype == 0x00:
            pairs = decode_digitals(body)
            if self._collecting is not None:
                # An entry is in flight. Collect rather than publish: the diff
                # against the previous state happens once the dump is complete,
                # so a re-asserted value is not reported as a change and an
                # omitted one is reported as off.
                self._collecting.update(pairs)
                return
            bucket = self.digital_for(self.current_subsystem)
            for join, value in pairs:
                previous = bucket.get(join)
                bucket[join] = value
                if previous != value:
                    self._on_digital(join, value, self.current_subsystem)
        elif datatype == 0x14:
            self.analog_rx += 1
            bucket = self.analog_for(self._incoming)
            for i in range(0, len(body) - 3, 4):
                join = ((body[i] << 8) | body[i + 1]) + 1
                bucket[join] = (body[i + 2] << 8) | body[i + 3]
        elif datatype == 0x01:
            self.analog_rx += 1
            self.analog_for(self._incoming)[body[0] + 1] = (body[1] << 8) | body[2]
        elif datatype == 0x15:
            # The join is 0-based on the wire like every other type here; this
            # branch was the one place that once missed the +1. Serials are not
            # bucketed: the sixty-five lighting load names arrive once per
            # session rather than once per entry, so bucketing them would lose
            # them on the first subsystem switch.
            self.serial[((body[0] << 8) | body[1]) + 1] = body[3:].decode("latin-1")
        elif datatype == 0x02:
            text = body.decode("latin-1")
            head = text.split(",", 1)[0].lstrip("#")
            self.serial[int(head) if head.isdigit() else 0] = text
        elif datatype == 0x03 and body and body[0] == 0x1C:
            await self._send(END_OF_QUERY_ACK)
            await self._send(HEARTBEAT)
            self._end_of_query.set()
            self._start_bring_up()

    # ---- writing -----------------------------------------------------------

    async def _send(self, packet: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise OSError(f"{self.name}: not connected")
        async with self._write_lock:
            writer.write(packet)
            await writer.drain()

    async def _press(self, join: int, hold: float = PRESS_HOLD_SECONDS) -> None:
        """Tap a digital join, checked against the joins that must never be written.

        This is one of two checks on the alarm range, and the only one that
        covers every write: this is the last point before bytes go on the wire.
        Entry presses and A/V presses do not come from the load table at all, so
        const._validate(), the other check, never sees them.

        There was a third, in the bridge, applying _validate()'s own predicate
        to _validate()'s own data one call later. It could not fire: any table
        that would have tripped it fails at import, so the module never loads.
        Deleted rather than left to read like defence in depth, because the next
        person auditing this needs to know that the check below is the one doing
        the work. See the pdehlke/homeassistant repo, issue #26.
        """
        if join in self.forbidden:
            raise CrestronError(
                f"{self.name}: refusing to press d{join}, shared with the DSC alarm keypad"
            )
        # Captured now rather than re-read in the finally. _close() nulls
        # self._writer *before* it cancels the bring-up task, so a press
        # cancelled by exactly the path this fallback exists for used to find
        # nothing there and leave the join held down.
        writer = self._writer
        released = False
        try:
            # Inside the try, because StreamWriter.drain() yields when the
            # transport is closing, which is the moment _close() cancels us. A
            # cancellation landing there leaves the press queued, and sending it
            # outside the try meant the release below was never reached.
            await self._send(digital_packet(join, True))
            await asyncio.sleep(hold)
            await self._send(digital_packet(join, False))
            released = True
        finally:
            if not released and writer is not None:
                # A press that is never released is a press-and-hold as far as
                # the processor is concerned, and holding is not a no-op on this
                # system: it ramps a dimmer, and on a learnable scene button it
                # overwrites the scene. Cancellation mid-hold is real, because
                # _close() cancels the bring-up task and that task presses.
                #
                # The release is queued straight onto the writer rather than
                # through _send(), because every await in _send is a point where
                # a cancellation already in flight would preempt it. write() is
                # not a coroutine and appends a whole packet, so it cannot be
                # interrupted or interleaved part-way.
                #
                # Releasing a join that never actually went down is a no-op on
                # the wire, so it is safe to do this even when the press itself
                # is what failed.
                with contextlib.suppress(Exception):
                    writer.write(digital_packet(join, False))

    async def async_press(
        self, join: int, subsystem: str | None, hold: float = PRESS_HOLD_SECONDS
    ) -> None:
        """Tap a digital join, refusing unless the slot is in the named subsystem.

        The subsystem is not optional and not inferred. `d101` is Dining Room
        Table in Lights and the AppleTV menu in A/V, so a caller that has not
        said which one it means has not said what it wants pressed.
        """
        if self.subsystems and self.current_subsystem != subsystem:
            raise CrestronError(
                f"{self.name}: refusing to press d{join} for {subsystem}, the slot is in "
                f"{self.current_subsystem or 'no'} subsystem"
            )
        await self._press(join, hold)
