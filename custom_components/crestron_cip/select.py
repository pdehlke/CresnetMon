"""Source selection, one per audio zone.

Stays available on a room that is off, unlike volume and mute, because picking
a source is the only way the hardware has of powering a room on. A room that is
off reports None, which Home Assistant shows as unknown, and that is the honest
answer: powering a zone off clears its source rather than remembering it.

There is deliberately no "Off" option. The power switch owns power; a second
control for the same thing is how two controls start disagreeing about it.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .bridge import CrestronBridge
from .const import AV_SOURCE_NAMES, AV_SOURCE_NUMBERS, DOMAIN, ZONES, Zone
from .entity import CrestronAvEntity


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    bridge: CrestronBridge = hass.data[DOMAIN]
    add_entities([CrestronZoneSource(bridge, zone) for zone in ZONES])


class CrestronZoneSource(CrestronAvEntity, SelectEntity):
    """Which of the six sources one zone is playing."""

    _attr_icon = "mdi:music-box-multiple"
    _attr_options = [AV_SOURCE_NAMES[n] for n in sorted(AV_SOURCE_NAMES)]

    def __init__(self, bridge: CrestronBridge, zone: Zone) -> None:
        super().__init__(bridge, zone)
        self._attr_unique_id = f"{DOMAIN}_av_{zone.key}_source"
        self._attr_name = f"Crestron {zone.name} Source"
        self.entity_id = f"select.crestron_{zone.key}_source"

    @property
    def current_option(self) -> str | None:
        state = self.zone_state
        if state is None:
            return None
        return AV_SOURCE_NAMES.get(state.get("source"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.zone_state or {}
        return {
            **super().extra_state_attributes,
            "source_number": state.get("source"),
            # What the processor calls this source, from s(100+N), the per-source
            # name serial. Kept alongside our own label because the two
            # disagreeing is the first sign AV_SOURCE_NAMES has drifted from the
            # AADS, and because the Integra's labels are known not to match what
            # they select.
            #
            # Deliberately NOT s16, the "selected source name" serial, which was
            # what this read first. Serials are not reliably refreshed on a
            # subsystem switch, and s16 in particular was seen still reading
            # 'Lights' after the slot had returned to A/V. Confirmed live again
            # on 2026-09-29: selecting Tuner 1 in Studio left s16 reading
            # 'Lights', so this attribute would have shown the lighting
            # subsystem's name as the room's source. The snapshot still carries
            # s16 as selected_source_name for anyone who wants it.
            "processor_source_name": state.get("source_name") or None,
        }

    async def async_select_option(self, option: str) -> None:
        source = AV_SOURCE_NUMBERS[option]
        # Keeping the volume rather than letting the source's preset stand:
        # Tuner 1's preset measured 40%, which on these speakers is silence, and
        # a source change that leaves a room inaudible reads as broken hardware.
        await self.run(self._av.async_select_source_keeping_volume(self._zone.key, source))
