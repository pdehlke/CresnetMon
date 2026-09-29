"""Shared plumbing for the audio-zone entities.

The four A/V platforms all want the same four things: read this zone's last
known state, redraw when it moves, refuse to act when the link is down, and
turn a CrestronError into something Home Assistant will show the user. One base
rather than four copies, for the same reason the `mac/` scripts ended up with
one shared alarm guard: four hand-copied versions is how two of them ended up
with none.

None of these entities poll. A/V state does not arrive by itself: the joins that
carry it are dropped before they reach a listener, deliberately, because the A/V
pages reuse join numbers that mean lighting loads in the other subsystem (see
bridge._on_digital). State moves only when something reads a zone, so the cache
in AvController is the whole source of truth and subscribing to it is the whole
update mechanism.
"""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from .bridge import CrestronBridge, CrestronError
from .const import LINK_AADS, Zone


class CrestronAvEntity(Entity):
    """One control for one audio zone."""

    _attr_should_poll = False

    def __init__(self, bridge: CrestronBridge, zone: Zone) -> None:
        self._bridge = bridge
        self._av = bridge.av
        self._zone = zone
        self._unsubscribe = None

    async def async_added_to_hass(self) -> None:
        @callback
        def _updated() -> None:
            self.async_write_ha_state()

        self._unsubscribe = self._av.subscribe(_updated)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    # ---- what the zone currently says --------------------------------------

    @property
    def zone_state(self) -> dict[str, object] | None:
        """This zone's last snapshot, or None if it has never been read."""
        return self._av.state(self._zone.key)

    @property
    def is_powered(self) -> bool | None:
        state = self.zone_state
        return None if state is None else bool(state.get("powered"))

    @property
    def link_up(self) -> bool:
        return self._bridge.link_connected(LINK_AADS)

    @property
    def available(self) -> bool:
        """Available whenever the link is up, read or not.

        Deliberately not conditioned on having a cached state. A zone nobody has
        read yet is a zone of unknown state, which the entity says by reporting
        None, and a control you cannot press is the wrong answer to "we have not
        looked yet" when pressing it is what would make us look.

        The volume and mute entities narrow this further; power and source do
        not, because those two are how an unknown or powered-off room is acted
        on at all.
        """
        return self.link_up

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        read_at = self._av.read_at(self._zone.key)
        return {
            "zone": self._zone.key,
            "last_read": (
                None if read_at is None else dt_util.utc_from_timestamp(read_at).isoformat()
            ),
        }

    # ---- acting ------------------------------------------------------------

    async def run(self, coro) -> None:
        """Await a bridge call and surface its failure to the user.

        A control that fails silently is worse than one that is not there: the
        room does not change and the toggle springs back with no explanation.
        HomeAssistantError is what puts the processor's own message in front of
        whoever pressed it.
        """
        try:
            await coro
        except CrestronError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_update(self) -> None:
        """Re-read this zone, for homeassistant.update_entity.

        Costs a cursor move, about half a second, and nothing when the cursor is
        already here. This is the per-room half of the refresh story; the
        all-six walk is button.crestron_audio_refresh.
        """
        await self.run(self._av.async_refresh(self._zone.key))
