"""Power and mute, one pair per audio zone.

Power is a switch even though the hardware has no power-on join, because
selecting a source *is* powering on and there is no other way to express it.
async_turn_on therefore means "select this zone's default source and set the
zone's own level", which is why the level lives on the Zone record rather than
here: every route into "on" has to agree about what the Kitchen means.

Mute is a switch because d48 is a toggle with its own feedback on d46, which is
exactly a switch. It goes unavailable with the room, because a muted state on a
room with no source selected is not a thing the hardware has.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .bridge import CrestronBridge
from .const import DOMAIN, ZONES, Zone
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
    entities: list[SwitchEntity] = []
    for zone in ZONES:
        entities.append(CrestronZonePower(bridge, zone))
        entities.append(CrestronZoneMute(bridge, zone))
    add_entities(entities)


class CrestronZonePower(CrestronAvEntity, SwitchEntity):
    """Whether one audio zone is playing anything at all."""

    _attr_icon = "mdi:speaker"

    def __init__(self, bridge: CrestronBridge, zone: Zone) -> None:
        super().__init__(bridge, zone)
        self._attr_unique_id = f"{DOMAIN}_av_{zone.key}_power"
        self._attr_name = f"Crestron {zone.name} Audio"
        # Explicit rather than slugified, matching binary_sensor.py: these ids
        # are what the Speakers dashboard and script.all_rooms_airplay bind to.
        self.entity_id = f"switch.crestron_{zone.key}_audio"

    @property
    def is_on(self) -> bool | None:
        return self.is_powered

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.zone_state or {}
        return {
            **super().extra_state_attributes,
            "source": state.get("source"),
            "source_name": state.get("source_name"),
            "on_volume": self._zone.on_volume,
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self.run(self._av.async_turn_on(self._zone.key))

    async def async_turn_off(self, **kwargs) -> None:
        await self.run(self._av.async_power_off(self._zone.key))


class CrestronZoneMute(CrestronAvEntity, SwitchEntity):
    """Mute state for one audio zone, from d46 and pressed on d48."""

    _attr_icon = "mdi:volume-off"

    def __init__(self, bridge: CrestronBridge, zone: Zone) -> None:
        super().__init__(bridge, zone)
        self._attr_unique_id = f"{DOMAIN}_av_{zone.key}_mute"
        self._attr_name = f"Crestron {zone.name} Mute"
        self.entity_id = f"switch.crestron_{zone.key}_mute"

    @property
    def available(self) -> bool:
        """Unavailable on a room that is off, or that has never been read.

        Powering a zone off clears its source rather than silencing it, so
        "muted" has nothing to describe on an off room, and whether d48 even
        does anything there is untested. Offering the control anyway would be
        inviting a press whose behaviour nobody has established.
        """
        return self.link_up and self.is_powered is True

    @property
    def is_on(self) -> bool | None:
        state = self.zone_state
        return None if state is None else bool(state.get("muted"))

    async def async_turn_on(self, **kwargs) -> None:
        await self.run(self._av.async_set_mute(self._zone.key, True))

    async def async_turn_off(self, **kwargs) -> None:
        await self.run(self._av.async_set_mute(self._zone.key, False))
