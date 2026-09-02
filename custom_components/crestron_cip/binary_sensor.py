"""Feedback entities: one per lighting load, plus one per CIP link.

These carry the truth about the house. The `light.*` entities read them, so the
whole receive path can be proven before anything is ever written.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .bridge import CrestronBridge
from .const import DOMAIN, LINK_AADS, LINK_MC2E, LOADS, Load


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    bridge: CrestronBridge = hass.data[DOMAIN]
    entities: list[BinarySensorEntity] = [CrestronLoadSensor(bridge, load) for load in LOADS]
    entities += [CrestronLinkSensor(bridge, link) for link in (LINK_AADS, LINK_MC2E)]
    add_entities(entities)


class _BridgeEntity(BinarySensorEntity):
    """Shared plumbing: no polling, redraw whenever the bridge says something moved."""

    _attr_should_poll = False

    def __init__(self, bridge: CrestronBridge) -> None:
        self._bridge = bridge
        self._unsubscribe = None

    async def async_added_to_hass(self) -> None:
        @callback
        def _updated() -> None:
            self.async_write_ha_state()

        self._unsubscribe = self._bridge.add_listener(_updated)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None


class CrestronLoadSensor(_BridgeEntity):
    """Whether one lighting load is currently on, as the processor reports it."""

    _attr_device_class = BinarySensorDeviceClass.LIGHT

    def __init__(self, bridge: CrestronBridge, load: Load) -> None:
        super().__init__(bridge)
        self._load = load
        self._attr_unique_id = f"{DOMAIN}_{load.key}"
        self._attr_name = f"Crestron {load.name}"
        # Deliberately explicit rather than left to slugification, because these
        # entity IDs are what the light entities bind to.
        self.entity_id = f"binary_sensor.crestron_{load.key}"

    @property
    def available(self) -> bool:
        return self._bridge.is_available(self._load.key)

    @property
    def is_on(self) -> bool | None:
        return self._bridge.is_on(self._load.key)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "link": self._load.link,
            "join": self._load.join,
            "alias_joins": list(self._load.aliases),
        }


class CrestronLinkSensor(_BridgeEntity):
    """Whether one CIP session is registered and synced."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_registry_enabled_default = True

    def __init__(self, bridge: CrestronBridge, link: str) -> None:
        super().__init__(bridge)
        self._link = link
        self._attr_unique_id = f"{DOMAIN}_link_{link}"
        self._attr_name = f"Crestron {link.upper()} link"
        self.entity_id = f"binary_sensor.crestron_link_{link}"

    @property
    def is_on(self) -> bool:
        return self._bridge.link_connected(self._link)
