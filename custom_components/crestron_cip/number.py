"""Volume, one per audio zone.

The slider spans 70 to 100 rather than 0 to 100. The services still take the
full range, because the hardware does, but below the audible floor of about 80%
the travel is silence and a control that spends four fifths of itself on "off,
but slowly" tells the user less than one that admits where the useful span is.
70 rather than 80 so the floor itself is reachable, and a little below it.

Goes unavailable with the room. A powered-off zone has no source, may have had
its level zeroed by the power-off, and whether a11 can be ramped at all in that
state is untested.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .bridge import CrestronBridge
from .const import (
    DOMAIN,
    VOLUME_SLIDER_MAX_PERCENT,
    VOLUME_SLIDER_MIN_PERCENT,
    VOLUME_SLIDER_STEP_PERCENT,
    ZONES,
    Zone,
)
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
    add_entities([CrestronZoneVolume(bridge, zone) for zone in ZONES])


class CrestronZoneVolume(CrestronAvEntity, NumberEntity):
    """One zone's level, as a percentage of full scale."""

    _attr_icon = "mdi:volume-high"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = VOLUME_SLIDER_MIN_PERCENT
    _attr_native_max_value = VOLUME_SLIDER_MAX_PERCENT
    _attr_native_step = VOLUME_SLIDER_STEP_PERCENT
    _attr_mode = NumberMode.SLIDER

    def __init__(self, bridge: CrestronBridge, zone: Zone) -> None:
        super().__init__(bridge, zone)
        self._attr_unique_id = f"{DOMAIN}_av_{zone.key}_volume"
        self._attr_name = f"Crestron {zone.name} Volume"
        self.entity_id = f"number.crestron_{zone.key}_volume"

    @property
    def available(self) -> bool:
        return self.link_up and self.is_powered is True

    @property
    def native_value(self) -> float | None:
        state = self.zone_state
        if state is None:
            return None
        percent = state.get("volume_percent")
        if percent is None:
            return None
        # Reported as the processor has it, not clamped into the slider's span.
        # A physical panel can leave a room at 40% and the number that says so
        # is the useful one; a slider showing 70 because 70 is as low as it goes
        # would be the control lying about the room to flatter itself.
        return round(float(percent), 1)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.zone_state or {}
        return {
            **super().extra_state_attributes,
            "raw": state.get("volume"),
            "on_volume": self._zone.on_volume,
        }

    async def async_set_native_value(self, value: float) -> None:
        await self.run(self._av.async_set_volume(self._zone.key, float(value)))
