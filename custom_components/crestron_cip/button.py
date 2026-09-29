"""The all-six refresh.

A/V state does not arrive by itself and nothing polls for it, so between one
Home Assistant action and the next a room can be changed from a wall panel and
nothing here would know. This is the control that goes and looks.

Costs a walk of the cursor across all six zones, roughly 4.7 seconds: about 1.9
for the first, which pays the subsystem entry, and about 0.55 for each of the
rest. The slot is handed back between zones, so a lighting command queued behind
this waits for one zone rather than for the whole walk.

Not a switch and not an automation-friendly service, because this is a thing you
press, which is exactly what a button entity is.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .bridge import CrestronBridge, CrestronError
from .const import DOMAIN, LINK_AADS


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    bridge: CrestronBridge = hass.data[DOMAIN]
    add_entities([CrestronAudioRefresh(bridge)])


class CrestronAudioRefresh(ButtonEntity):
    """Re-read all six audio zones."""

    _attr_should_poll = False
    _attr_icon = "mdi:refresh"
    _attr_name = "Crestron Audio Refresh"

    def __init__(self, bridge: CrestronBridge) -> None:
        self._bridge = bridge
        self._av = bridge.av
        self._attr_unique_id = f"{DOMAIN}_av_refresh"
        self.entity_id = "button.crestron_audio_refresh"
        self._unsubscribe = None

    async def async_added_to_hass(self) -> None:
        self._unsubscribe = self._av.subscribe(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def available(self) -> bool:
        return self._bridge.link_connected(LINK_AADS)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Carries the page-level staleness the Speakers dashboard shows.

        One line for the whole page rather than six per-room ones, because a
        single press refreshes all six and six copies of the same complaint is
        six times the noise for no extra information.

        None until every zone has been read at least once, so a dashboard that
        is still half blank does not get to describe itself as fresh.
        """
        oldest = self._av.oldest_read_at()
        return {
            "oldest_read": (
                None if oldest is None else dt_util.utc_from_timestamp(oldest).isoformat()
            ),
        }

    async def async_press(self) -> None:
        # async_refresh_all swallows a per-zone CrestronError and logs it, so
        # one unreachable zone costs that zone and not the other five. This
        # catch is for a failure that takes the walk itself down, such as the
        # link dropping between zones.
        try:
            await self._av.async_refresh_all()
        except CrestronError as err:
            raise HomeAssistantError(str(err)) from err
