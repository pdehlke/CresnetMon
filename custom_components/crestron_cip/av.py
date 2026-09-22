"""Room-by-room audio over the same panel slot the lighting bridge holds.

The AADS's six audio zones are reached through one cursor. A slot selects a
zone, and from then on the source, volume and power joins act on that zone and
the volume join reports it. The cursor is per slot, so Home Assistant has its
own and moving it does not drag the physical panels around, but it also means
only one zone is readable at a time. Nothing here pretends otherwise: every
operation names the zone it wants and moves the cursor to it first.

Three constraints from the live mapping shape everything below, all recorded in
the pdehlke/homeassistant repo at docs/crestron/crestron-av-zone-control-path.md:

  * Selecting a source powers the zone on and overwrites the volume with a
    per-source preset, so volume is always set after a source, never before.
  * a11 accepts no direct write. Setting a level means holding d44 or d45 for a
    computed time and converging against what a11 then reports.
  * A cursor move blanks the per-zone joins for about 60ms. Reading inside that
    window reports a dead zone with complete confidence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .cip import CipClient, CrestronError
from .const import (
    ALL_ZONES_OFF_JOIN,
    AV_NO_SOURCE_JOIN,
    AV_SOURCES,
    CURSOR_CONFIRM_TIMEOUT,
    CURSOR_SETTLE_SECONDS,
    MAX_HOLD_SECONDS,
    MUTE_FEEDBACK_JOIN,
    MUTE_JOIN,
    SOURCE_NAME_SERIAL,
    SUBSYSTEM_AV,
    VOLUME_ANALOG,
    VOLUME_AUDIBLE_FLOOR_PERCENT,
    VOLUME_DOWN_JOIN,
    VOLUME_FULL_SCALE,
    VOLUME_MAX_SEGMENTS,
    VOLUME_RAMP_UNITS_PER_SECOND,
    VOLUME_TOLERANCE,
    VOLUME_UP_JOIN,
    ZONE_NAME_SERIAL,
    ZONE_POWER_OFF_JOIN,
    ZONES_BY_KEY,
    Zone,
    source_feedback_join,
    source_name_serial,
    source_press_join,
)

_LOGGER = logging.getLogger(__name__)


def percent_to_raw(percent: float) -> int:
    return round(percent / 100.0 * VOLUME_FULL_SCALE)


def raw_to_percent(raw: int) -> float:
    return round(raw / VOLUME_FULL_SCALE * 100.0, 1)


class AvController:
    """Drives the six audio zones through one shared panel slot.

    Holds the same per-link lock the lighting commands use, so a lighting write
    can never go out while the slot sits in A/V, and gives it back between
    operations. Nothing here holds it for longer than MAX_HOLD_SECONDS, which is
    what keeps a nine-second volume ramp from meaning nine seconds of lighting
    latency.
    """

    def __init__(self, client: CipClient, lock: asyncio.Lock, touch: Callable[[], None]) -> None:
        self._client = client
        self._lock = lock
        self._touch = touch
        self._cursor: str | None = None
        self._cursor_generation = -1

    # ---- reading -----------------------------------------------------------

    def _digital(self, join: int) -> int:
        return self._client.digital_for(SUBSYSTEM_AV).get(join, 0)

    def _selected_source(self) -> int | None:
        """Which source the cursor's zone is playing, or None for none.

        Read from d51-d56, the same joins that take the presses, because those
        are the per-zone feedback and are among the joins a cursor move blanks
        and repopulates.

        Not from the d10N1 family, which was the first reading here and is
        wrong. Those select the per-source display page, so they are slot state
        rather than zone state: after AirPlay was selected in Studio on
        2026-09-22, d1021 stayed high as the cursor moved and every other zone
        read back as playing AirPlay while all five were off.
        """
        for source in AV_SOURCES:
            if self._digital(source_press_join(source)):
                return source
        return None

    def _snapshot(self, zone: Zone) -> dict[str, object]:
        source = self._selected_source()
        raw = self._client.analog_for(SUBSYSTEM_AV).get(VOLUME_ANALOG)
        return {
            "zone": zone.key,
            "zone_name": zone.name,
            "reported_zone": self._client.serial.get(ZONE_NAME_SERIAL, "").strip(),
            "source": source,
            "source_name": (
                self._client.serial.get(source_name_serial(source), "").strip() if source else None
            ),
            "selected_source_name": self._client.serial.get(SOURCE_NAME_SERIAL, "").strip(),
            "powered": source is not None,
            # Slot state, not zone state. Kept in the response because it is
            # what the panel is displaying and it made a liar of this module
            # once already.
            "displayed_source_page": next(
                (n for n in AV_SOURCES if self._digital(source_feedback_join(n))),
                0 if self._digital(AV_NO_SOURCE_JOIN) else None,
            ),
            "muted": bool(self._digital(MUTE_FEEDBACK_JOIN)),
            "volume": raw,
            "volume_percent": raw_to_percent(raw) if raw is not None else None,
        }

    # ---- the cursor --------------------------------------------------------

    async def _async_enter(self) -> None:
        if not (self._client.connected and self._client.synced):
            raise CrestronError(f"{self._client.name}: link is not connected")
        if not await self._client.async_enter(SUBSYSTEM_AV):
            raise CrestronError(f"{self._client.name}: could not enter the A/V subsystem")

    async def _async_point_at(self, zone: Zone) -> None:
        """Put the cursor on one zone, and be sure it landed before reading.

        Skipped when the cursor is known to be there already, which is only
        knowable within one session: no physical panel can move this slot's
        cursor, so the cache is exact until the session is replaced.
        """
        if self._cursor == zone.key and self._cursor_generation == self._client.generation:
            return

        self._cursor = None
        analog_before = self._client.analog_rx
        await self._client.async_press(zone.select_join, SUBSYSTEM_AV)
        await asyncio.sleep(CURSOR_SETTLE_SECONDS)

        # Two things have to be true before the zone can be read, and they do not
        # arrive together. s11 has to name the zone, and the processor has to
        # have said something about the analogs since the press, or a11 still
        # describes the zone the cursor came from.
        #
        # The second condition has a grace period rather than a hard wait,
        # because a zone whose level matches the one before it produces no frame
        # to wait for. Master Bed and Master Bath both sat at 61018 on
        # 2026-09-22 and the move between them sent nothing at all. When that
        # happens the value already in hand is the right one, since it is the
        # same number either way.
        #
        # Clearing a11 before the press was tried and is wrong: the 60ms blank
        # covers the digital joins, d41/d43/d47 and d51-d56, not the analog, so
        # clearing it discards a value the processor has no reason to resend.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CURSOR_CONFIRM_TIMEOUT
        named_at: float | None = None
        while loop.time() < deadline:
            reported = self._client.serial.get(ZONE_NAME_SERIAL, "").strip()
            if reported.casefold() == zone.name.casefold():
                named_at = named_at or loop.time()
                spoke = self._client.analog_rx != analog_before
                quiet_long_enough = loop.time() - named_at >= CURSOR_SETTLE_SECONDS
                if (spoke or quiet_long_enough) and VOLUME_ANALOG in self._client.analog_for(
                    SUBSYSTEM_AV
                ):
                    self._cursor = zone.key
                    self._cursor_generation = self._client.generation
                    return
            await asyncio.sleep(0.05)

        # s11 is a serial, and serials are not reliably refreshed on a subsystem
        # switch: s16 was seen still reading 'Lights' after the slot had returned
        # to A/V. So a mismatch is not proof the cursor failed to move, and
        # refusing here would be refusing on weak evidence. Proceed, but say so,
        # because the alternative reading is that every value below belongs to
        # the wrong room.
        _LOGGER.warning(
            "%s: cursor move to %s unconfirmed after %.1fs (s11 reads %r, a%d %s); "
            "proceeding on the press alone",
            self._client.name,
            zone.name,
            CURSOR_CONFIRM_TIMEOUT,
            self._client.serial.get(ZONE_NAME_SERIAL, ""),
            VOLUME_ANALOG,
            "present" if VOLUME_ANALOG in self._client.analog_for(SUBSYSTEM_AV) else "absent",
        )
        self._cursor = zone.key
        self._cursor_generation = self._client.generation

    def _zone(self, key: str) -> Zone:
        zone = ZONES_BY_KEY.get(key)
        if zone is None:
            raise CrestronError(
                f"unknown audio zone {key!r}, expected one of {sorted(ZONES_BY_KEY)}"
            )
        return zone

    # ---- operations --------------------------------------------------------

    async def async_status(self, zone_key: str) -> dict[str, object]:
        """Read one zone. Costs a cursor move unless the cursor is already there."""
        zone = self._zone(zone_key)
        async with self._lock:
            try:
                await self._async_enter()
                await self._async_point_at(zone)
                return self._snapshot(zone)
            finally:
                self._touch()

    async def async_select_source(self, zone_key: str, source: int) -> dict[str, object]:
        """Select a source, which also powers the zone on.

        There is no separate power-on join to look for: source select and power
        on are one action on this system. The volume the zone lands at is the
        AADS's per-source preset, not whatever it was before, so a caller that
        wants a level must set it after this and not before.
        """
        zone = self._zone(zone_key)
        if source not in AV_SOURCES:
            raise CrestronError(f"source {source} is not one of {list(AV_SOURCES)}")

        async with self._lock:
            try:
                await self._async_enter()
                await self._async_point_at(zone)
                if self._selected_source() == source:
                    return self._snapshot(zone)

                press = source_press_join(source)
                await self._client.async_press(press, SUBSYSTEM_AV)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + CURSOR_CONFIRM_TIMEOUT
                while loop.time() < deadline:
                    if self._digital(press):
                        return self._snapshot(zone)
                    await asyncio.sleep(0.05)
                raise CrestronError(
                    f"{zone.key}: pressed d{press} for source {source} and it never came high"
                )
            finally:
                self._touch()

    async def async_set_volume(self, zone_key: str, percent: float) -> dict[str, object]:
        """Ramp one zone to a target level and converge on it.

        Open loop then corrected, because the only control is a ramp and the
        only readback is a11. Each segment takes the lock, presses for at most
        MAX_HOLD_SECONDS, and gives the lock back, so a lighting command queued
        behind a long ramp waits for one segment rather than the whole ramp.
        Releasing mid-ramp is safe: the cursor and the level both survive a
        subsystem round trip, so a ramp interrupted by a lighting write picks up
        where it left off.
        """
        zone = self._zone(zone_key)
        if not 0.0 <= percent <= 100.0:
            raise CrestronError(f"volume {percent} is outside 0-100")
        if 0 < percent < VOLUME_AUDIBLE_FLOOR_PERCENT:
            _LOGGER.warning(
                "%s: asked for %.1f%%, below the ~%.0f%% these speakers become audible at",
                zone.key,
                percent,
                VOLUME_AUDIBLE_FLOOR_PERCENT,
            )
        target = percent_to_raw(percent)

        for segment in range(1, VOLUME_MAX_SEGMENTS + 1):
            async with self._lock:
                try:
                    await self._async_enter()
                    await self._async_point_at(zone)
                    current = self._client.analog_for(SUBSYSTEM_AV).get(VOLUME_ANALOG)
                    if current is None:
                        raise CrestronError(
                            f"{zone.key}: no volume reported on a{VOLUME_ANALOG}, refusing to "
                            "ramp blind"
                        )

                    delta = target - current
                    if abs(delta) <= VOLUME_TOLERANCE:
                        _LOGGER.debug(
                            "%s: volume settled at %d, wanted %d, after %d segment(s)",
                            zone.key,
                            current,
                            target,
                            segment - 1,
                        )
                        return self._snapshot(zone)

                    hold = min(MAX_HOLD_SECONDS, abs(delta) / VOLUME_RAMP_UNITS_PER_SECOND)
                    join = VOLUME_UP_JOIN if delta > 0 else VOLUME_DOWN_JOIN
                    _LOGGER.debug(
                        "%s: at %d, want %d, holding d%d for %.2fs (segment %d)",
                        zone.key,
                        current,
                        target,
                        join,
                        hold,
                        segment,
                    )
                    await self._client.async_press(join, SUBSYSTEM_AV, hold=hold)
                    await asyncio.sleep(CURSOR_SETTLE_SECONDS)
                finally:
                    self._touch()

        final = self._client.analog_for(SUBSYSTEM_AV).get(VOLUME_ANALOG)
        raise CrestronError(
            f"{zone.key}: volume stalled at {final} after {VOLUME_MAX_SEGMENTS} segments, "
            f"wanted {target}"
        )

    async def async_power_off(self, zone_key: str) -> dict[str, object]:
        """Power one zone off.

        This clears the zone's source selection rather than only silencing it,
        verified persistent across a cursor move. There is no join path back to
        "off but remembering its source", because restoring the source powers the
        zone on again.
        """
        zone = self._zone(zone_key)
        async with self._lock:
            try:
                await self._async_enter()
                await self._async_point_at(zone)
                if self._selected_source() is None and self._digital(ZONE_POWER_OFF_JOIN):
                    return self._snapshot(zone)
                await self._client.async_press(ZONE_POWER_OFF_JOIN, SUBSYSTEM_AV)
                await asyncio.sleep(CURSOR_SETTLE_SECONDS)
                return self._snapshot(zone)
            finally:
                self._touch()

    async def async_power_off_all(self) -> None:
        """Power every zone off at once, with one press rather than six visits."""
        async with self._lock:
            try:
                await self._async_enter()
                await self._client.async_press(ALL_ZONES_OFF_JOIN, SUBSYSTEM_AV)
                await asyncio.sleep(CURSOR_SETTLE_SECONDS)
                # Every zone's state just changed, and only the cursor's is
                # readable, so there is nothing honest to return here.
                self._cursor = None
            finally:
                self._touch()

    async def async_set_mute(self, zone_key: str, mute: bool) -> dict[str, object]:
        """Mute or unmute one zone. d48 is a toggle, so consult d46 first."""
        zone = self._zone(zone_key)
        async with self._lock:
            try:
                await self._async_enter()
                await self._async_point_at(zone)
                if bool(self._digital(MUTE_FEEDBACK_JOIN)) == mute:
                    return self._snapshot(zone)
                await self._client.async_press(MUTE_JOIN, SUBSYSTEM_AV)
                await asyncio.sleep(CURSOR_SETTLE_SECONDS)
                return self._snapshot(zone)
            finally:
                self._touch()
