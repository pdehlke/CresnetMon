"""Load state and discrete on/off over Crestron's toggle-only buttons.

Every lighting button in the TSW-752 panel project is a momentary toggle. One
press flips the load and there is no per-load discrete off anywhere in the
project. Home Assistant needs discrete `turn_on` and `turn_off`, so this module
implements them: consult the live feedback state, press only when it differs from
what was asked for, then wait for the processor to confirm.

That decision has to be made here rather than in a template light's action list,
because it depends on the state the previous press produced and must be
serialized against feedback arriving from a wall panel at the same moment.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .cip import CipClient
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

_LOGGER = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 3.0
CONFIRM_ATTEMPTS = 2


class CrestronError(Exception):
    """Raised when a command cannot be carried out safely or at all."""


class CrestronBridge:
    """Owns both CIP links and presents one load-keyed view of the house."""

    def __init__(self, config: dict[str, dict]) -> None:
        self._clients: dict[str, CipClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[Callable[[], None]] = []
        self._waiters: dict[str, list[tuple[bool, asyncio.Future[None]]]] = {}

        # join -> load, per link. Aliases resolve to the same load, which is how
        # one physical light on five buttons stays one entity.
        self._by_join: dict[str, dict[int, Load]] = {LINK_AADS: {}, LINK_MC2E: {}}
        for load in LOADS:
            for join in load.joins:
                self._by_join[load.link][join] = load

        for link in (LINK_AADS, LINK_MC2E):
            settings = {**DEFAULTS[link], **config.get(link, {})}
            self._clients[link] = CipClient(
                name=link,
                host=settings["host"],
                port=settings.get("port", CIP_PORT),
                ipid=settings["ipid"],
                on_digital=lambda join, value, _link=link: self._on_digital(_link, join, value),
                on_state=self._notify,
            )
            self._locks[link] = asyncio.Lock()

    # ---- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        for client in self._clients.values():
            await client.async_start()

    async def async_stop(self) -> None:
        for client in self._clients.values():
            await client.async_stop()

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
        client = self._clients.get(link)
        return bool(client and client.connected and client.synced)

    def is_available(self, key: str) -> bool:
        """A load is available once its link has a synced session and a join.

        The four Kitchen loads have no join until the identification pass fills
        them in, so they report unavailable rather than guessing at a state.
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
        return bool(self._clients[load.link].digital.get(load.join, 0))

    def _on_digital(self, link: str, join: int, value: int) -> None:
        load = self._by_join[link].get(join)
        if load is None:
            return
        # An alias moving is the same event as the canonical join moving. Mirror
        # it onto the canonical join so is_on() has one place to read.
        if load.join is not None and join != load.join:
            self._clients[link].digital[load.join] = value
        self._resolve_waiters(load.key, bool(value))
        self._notify()

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

        client = self._clients[load.link]
        if not (client.connected and client.synced):
            raise CrestronError(f"{key}: {load.link} link is not connected")

        async with self._locks[load.link]:
            for attempt in range(1, CONFIRM_ATTEMPTS + 1):
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
                    await client.async_press(press_join)
                    await asyncio.wait_for(waiter, CONFIRM_TIMEOUT)
                    return
                except TimeoutError:
                    _LOGGER.warning(
                        "%s: no feedback within %.0fs of pressing d%d",
                        key,
                        CONFIRM_TIMEOUT,
                        press_join,
                    )
                finally:
                    self._drop_waiter(key, waiter)

        raise CrestronError(
            f"{key}: pressed {CONFIRM_ATTEMPTS} times without the processor "
            f"confirming {'on' if want_on else 'off'}"
        )

    def _guard(self, load: Load) -> None:
        """Refuse to write a join the DSC alarm keypad shares.

        const._validate() already rejects a table containing such a join at
        import, so reaching here means something constructed a Load at
        runtime. Check anyway: this is the last point before bytes go on the
        wire, unconditionally, whether or not this call ends up pressing
        anything. Checked against every join the load could ever press
        (`press_joins`), not just its canonical `join`, since press_on/press_off
        can differ from it.
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
