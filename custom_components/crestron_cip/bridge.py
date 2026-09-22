"""Load state and discrete on/off over Crestron's toggle-only buttons.

Every lighting button in the TSW-752 panel project is a momentary toggle. One
press flips the load and there is no per-load discrete off anywhere in the
project. Home Assistant needs discrete `turn_on` and `turn_off`, so this module
implements them: consult the live feedback state, press only when it differs from
what was asked for, then wait for the processor to confirm.

That decision has to be made here rather than in a template light's action list,
because it depends on the state the previous press produced and must be
serialized against feedback arriving from a wall panel at the same moment.

The per-link lock does a second job now that the AADS slot is shared between the
Lights and A/V subsystems. It serializes the subsystem the slot is in, not just
the presses, so a lighting write can never go out while the slot sits in A/V.
Operations are expected to hold it briefly and hand it back; asyncio.Lock wakes
waiters in order, so a lighting write queued behind an A/V operation runs as soon
as that operation ends and needs no interrupt protocol to get there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .av import AvController
from .cip import CipClient, CrestronError
from .const import (
    CIP_PORT,
    DEFAULTS,
    FORBIDDEN_AADS_WRITE,
    LINK_AADS,
    LINK_MC2E,
    LOADS,
    LOADS_BY_KEY,
    Load,
)
from .link import Link

_LOGGER = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 3.0
CONFIRM_ATTEMPTS = 2
IDLE_WATCH_INTERVAL = 1.0


class CrestronBridge:
    """Owns both CIP links and presents one load-keyed view of the house."""

    def __init__(self, config: dict[str, dict]) -> None:
        self._links: dict[str, Link] = {}
        self._listeners: list[Callable[[], None]] = []
        self._waiters: dict[str, list[tuple[bool, asyncio.Future[None]]]] = {}
        self._idle_task: asyncio.Task | None = None

        for name in (LINK_AADS, LINK_MC2E):
            settings = {**DEFAULTS[name], **config.get(name, {})}
            self._links[name] = Link(
                name,
                CipClient(
                    name=name,
                    host=settings["host"],
                    port=settings.get("port", CIP_PORT),
                    ipid=settings["ipid"],
                    subsystems=settings.get("subsystems"),
                    default_subsystem=settings.get("default_subsystem"),
                    forbidden=FORBIDDEN_AADS_WRITE if name == LINK_AADS else frozenset(),
                    on_digital=(
                        lambda join, value, subsystem, _link=name: self._on_digital(
                            _link, join, value, subsystem
                        )
                    ),
                    on_state=self._notify,
                ),
            )
        for load in LOADS:
            for join in load.joins:
                self._links[load.link].by_join[join] = load

        # Audio shares the AADS slot with lighting by taking turns, so it takes
        # that link rather than owning anything of its own.
        self.av = AvController(self._links[LINK_AADS])

    # ---- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        for link in self._links.values():
            await link.client.async_start()
        self._idle_task = asyncio.create_task(self._idle_watch(), name="crestron_cip-idle")

    async def async_stop(self) -> None:
        if self._idle_task:
            # Awaited, not just cancelled. The client teardown below yields, so
            # an unawaited idle task would take its CancelledError while
            # _close() is nulling the writer out from under whatever press it
            # was in the middle of. CipClient.async_stop() already does this.
            idle, self._idle_task = self._idle_task, None
            idle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle
        for link in self._links.values():
            await link.client.async_stop()

    # ---- observation -------------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    def link_connected(self, link: str) -> bool:
        known = self._links.get(link)
        return bool(known and known.connected)

    def subsystem(self, link: str) -> str | None:
        """Which subsystem the slot is in, or None for unknown or ungated."""
        known = self._links.get(link)
        return known.client.current_subsystem if known else None

    def is_available(self, key: str) -> bool:
        """A load is available once its link has a synced session and a join.

        The four Kitchen loads have no join until the identification pass fills
        them in, so they report unavailable rather than guessing at a state.

        Deliberately not conditioned on the slot's current subsystem. An A/V
        excursion lasts about a second and the state it freezes is corrected by
        the dump on the way back, so flapping thirty entities to unavailable and
        back would be worse than the stale read it replaces.
        """
        load = LOADS_BY_KEY[key]
        return load.join is not None and self.link_connected(load.link)

    def is_on(self, key: str) -> bool | None:
        """Current state, or None when it is genuinely not known."""
        load = LOADS_BY_KEY[key]
        if load.join is None or not self.link_connected(load.link):
            return None
        # The dump reports only high joins, so a load the processor never
        # mentioned is off, not unknown. That is only true once synced.
        return bool(self._links[load.link].client.digital.get(load.join, 0))

    def _on_digital(self, link: str, join: int, value: int, subsystem: str | None) -> None:
        known = self._links[link]
        client = known.client
        if subsystem != client.default_subsystem:
            # The A/V pages reuse d101-d109, d201-d204 and d151-d157 for things
            # that are not lights at all, so a join arriving from another
            # subsystem says nothing about a load and must not move one.
            return
        load = known.by_join.get(join)
        if load is None:
            return
        # An alias moving is the same event as the canonical join moving. Mirror
        # it onto the canonical join so is_on() has one place to read.
        if load.join is not None and join != load.join:
            client.digital[load.join] = value
        self._resolve_waiters(load.key, bool(value))
        self._notify()

    # ---- the shared slot ---------------------------------------------------

    async def async_enter_subsystem(
        self, link: str, subsystem: str, hold_seconds: float = 0.0
    ) -> dict[str, object]:
        """Switch one slot into a subsystem and say where it ended up.

        Exists so the switching machinery can be exercised and watched before any
        A/V service is built on top of it, and because step 2 needs exactly this
        call anyway. `hold_seconds` defers the idle return, which is what makes
        the excursion long enough to look at by hand.
        """
        known = self._links.get(link)
        if known is None:
            raise CrestronError(f"unknown link {link!r}")
        client = known.client
        if subsystem not in client.subsystems:
            names = ", ".join(sorted(client.subsystems)) or "none"
            raise CrestronError(f"{link} has no {subsystem!r} subsystem (knows: {names})")
        if not known.connected:
            raise CrestronError(f"{link} link is not connected")

        async with known.lock:
            entered = await client.async_enter(subsystem)
            known.touch(hold_seconds)
        if not entered:
            raise CrestronError(f"{link}: the {subsystem} subsystem did not answer the entry press")
        return {"link": link, "subsystem": client.current_subsystem}

    async def _idle_watch(self) -> None:
        """Return an idle slot to its default subsystem.

        Nothing in an A/V operation would otherwise ever switch back, and every
        second the slot spends away is a second in which a light changed at a
        wall panel is invisible here.

        Skips a link that is not synced: the only other caller of async_enter()
        that does not hold this lock is the client's own bring-up, which runs
        exactly while synced is false, and two entries at once would collide over
        the collection buffer.
        """
        while True:
            try:
                await asyncio.sleep(IDLE_WATCH_INTERVAL)
                for link in self._links.values():
                    client = link.client
                    default = client.default_subsystem
                    if default is None or not client.subsystems:
                        continue
                    if not link.connected or link.lock.locked() or not link.idle:
                        continue
                    # None means unknown, and recovering from that belongs to the
                    # next write or the bring-up, which both rebuild state anyway.
                    if client.current_subsystem in (None, default):
                        continue
                    async with link.lock:
                        # Rechecked in full. The pre-checks above are what make
                        # this acquire uncontended and therefore non-yielding,
                        # so nothing can change underneath today; a recheck that
                        # covered only the subsystem would quietly stop being
                        # enough the moment that skip became a wait.
                        if not link.connected:
                            continue
                        if client.current_subsystem in (None, default):
                            continue
                        _LOGGER.info(
                            "%s: idle in %s, returning to %s",
                            link.name,
                            client.current_subsystem,
                            default,
                        )
                        await client.async_enter(default)
                        link.touch()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("idle subsystem watch failed")

    # ---- commands ----------------------------------------------------------

    async def async_turn_on(self, key: str) -> None:
        await self._async_set(key, True)

    async def async_turn_off(self, key: str) -> None:
        await self._async_set(key, False)

    async def async_toggle(self, key: str) -> None:
        state = self.is_on(key)
        if state is None:
            raise CrestronError(f"{key}: state unknown, refusing to toggle blind")
        await self._async_set(key, not state)

    async def _async_set(self, key: str, want_on: bool) -> None:
        load = LOADS_BY_KEY.get(key)
        if load is None:
            raise CrestronError(f"unknown load {key!r}")
        if load.join is None:
            raise CrestronError(f"{key}: no join mapped yet, cannot control it")
        self._guard(load)

        link = self._links[load.link]
        client = link.client
        if not link.connected:
            raise CrestronError(f"{key}: {load.link} link is not connected")
        subsystem = client.default_subsystem

        async with link.lock:
            try:
                for attempt in range(1, CONFIRM_ATTEMPTS + 1):
                    # Revalidated every attempt, not just before the lock. The
                    # confirmation wait below is three seconds long, which is
                    # ample for the session to drop, reconnect and start a
                    # bring-up; carrying on into that would have this retry and
                    # the bring-up entering the slot at the same time. The
                    # client serialises the collection buffer itself now, so
                    # this is about failing the command honestly rather than
                    # about the buffer.
                    if not link.connected:
                        raise CrestronError(f"{key}: the {load.link} link dropped mid-command")
                    # First thing every attempt, because the state this reads
                    # next is only trustworthy once the subsystem's own dump has
                    # landed, and because a retry after an unconfirmed press gets
                    # here with the subsystem deliberately forgotten.
                    if not await client.async_enter(subsystem):
                        raise CrestronError(
                            f"{key}: could not enter the {subsystem} subsystem on {load.link}"
                        )

                    current = self.is_on(key)
                    if current is None:
                        raise CrestronError(f"{key}: state unknown, refusing to press blind")
                    if current == want_on:
                        if attempt > 1:
                            _LOGGER.debug("%s: confirmed %s on attempt %d", key, want_on, attempt)
                        return

                    press_join = load.press_join(want_on)
                    waiter = self._register_waiter(key, want_on)
                    _LOGGER.debug(
                        "%s: pressing d%d to go %s (attempt %d)",
                        key,
                        press_join,
                        "on" if want_on else "off",
                        attempt,
                    )
                    try:
                        await client.async_press(press_join, subsystem)
                        await asyncio.wait_for(waiter, CONFIRM_TIMEOUT)
                        return
                    except TimeoutError:
                        _LOGGER.warning(
                            "%s: no feedback within %.0fs of pressing d%d",
                            key,
                            CONFIRM_TIMEOUT,
                            press_join,
                        )
                        # The slot may have been dropped out of the subsystem
                        # without the session dropping, which is the one way this
                        # bridge could fail forever. Forget where it is so the
                        # next attempt re-enters and rebuilds rather than pressing
                        # into a slot that is not listening. Issue #25.
                        client.invalidate_subsystem()
                    finally:
                        self._drop_waiter(key, waiter)
            finally:
                link.touch()

        raise CrestronError(
            f"{key}: pressed {CONFIRM_ATTEMPTS} times without the processor "
            f"confirming {'on' if want_on else 'off'}"
        )

    def _guard(self, load: Load) -> None:
        """Refuse to write a join the DSC alarm keypad shares.

        const._validate() already rejects a table containing such a join at
        import, and CipClient._press() checks again with the bytes in hand, which
        is the check every write passes through including entry presses. This one
        stays because it names the load, and because three checks on the one
        thing in this system that must never be written is the right number.
        Checked against every join the load could ever press (`press_joins`), not
        just its canonical `join`, since press_on/press_off can differ from it.
        """
        if load.link == LINK_AADS and any(j in FORBIDDEN_AADS_WRITE for j in load.press_joins):
            raise CrestronError(f"refusing to press {load.key}: shared with the DSC alarm keypad")

    # ---- confirmation waiters ---------------------------------------------

    def _register_waiter(self, key: str, want_on: bool) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append((want_on, future))
        return future

    def _drop_waiter(self, key: str, future: asyncio.Future[None]) -> None:
        pending = self._waiters.get(key)
        if not pending:
            return
        self._waiters[key] = [entry for entry in pending if entry[1] is not future]
        if not self._waiters[key]:
            del self._waiters[key]

    def _resolve_waiters(self, key: str, value: bool) -> None:
        for want_on, future in list(self._waiters.get(key, ())):
            if want_on == value and not future.done():
                future.set_result(None)
