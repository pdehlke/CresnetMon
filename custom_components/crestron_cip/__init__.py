"""Crestron CIP lighting bridge.

Registers as a freed TSW-752 touch panel on the AADS and as the unoccupied XPanel
on the MC2E, and presents every lighting load as a feedback entity plus a discrete
on/off service. See the pdehlke/homeassistant repo,
docs/crestron/crestron-tsw-panel-control-path.md, for how the control path was
established and why it needs two connections.

Configured in YAML rather than through a config flow: this is a single-instance
integration for one house, and its addressing is fixed by the physical hardware.

    crestron_cip:

is enough. Hosts and IP-IDs may be overridden per link if anything moves.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .bridge import CrestronBridge, CrestronError
from .const import (
    AV_SOURCES,
    DOMAIN,
    ENTRY_JOINS,
    LINK_AADS,
    LINK_MC2E,
    LOADS_BY_KEY,
    ZONES_BY_KEY,
)

_LOGGER = logging.getLogger(__name__)

CONF_IPID = "ipid"
ATTR_LOAD = "load"
ATTR_LINK = "link"
ATTR_SUBSYSTEM = "subsystem"
ATTR_HOLD_SECONDS = "hold_seconds"
ATTR_ZONE = "zone"
ATTR_SOURCE = "source"
ATTR_VOLUME = "volume"
ATTR_MUTE = "mute"

_LINK_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HOST): cv.string,
        vol.Optional(CONF_IPID): vol.Coerce(int),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(LINK_AADS, default={}): _LINK_SCHEMA,
                vol.Optional(LINK_MC2E, default={}): _LINK_SCHEMA,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

_SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_LOAD): vol.In(sorted(LOADS_BY_KEY))})

_ENTER_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_LINK, default=LINK_AADS): vol.In([LINK_AADS, LINK_MC2E]),
        vol.Required(ATTR_SUBSYSTEM): vol.In(sorted(ENTRY_JOINS)),
        vol.Optional(ATTR_HOLD_SECONDS, default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=300)
        ),
    }
)


_ZONE = vol.In(sorted(ZONES_BY_KEY))
_ZONE_SCHEMA = vol.Schema({vol.Required(ATTR_ZONE): _ZONE})
_SOURCE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_ZONE): _ZONE, vol.Required(ATTR_SOURCE): vol.In(list(AV_SOURCES))}
)
_VOLUME_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ZONE): _ZONE,
        vol.Required(ATTR_VOLUME): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
    }
)
_MUTE_SCHEMA = vol.Schema({vol.Required(ATTR_ZONE): _ZONE, vol.Required(ATTR_MUTE): cv.boolean})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Start both CIP links and register the load services."""
    conf = config.get(DOMAIN) or {}
    bridge = CrestronBridge(conf)
    hass.data[DOMAIN] = bridge
    await bridge.async_start()

    def _service(method):
        """Wrap a bridge method as a service handler.

        This must return an `async def`, not a lambda that happens to return a
        coroutine. Home Assistant decides how to invoke a handler with
        asyncio.iscoroutinefunction(); a lambda fails that check, so HA runs it
        in an executor thread, gets a coroutine object back and drops it on the
        floor. The service then reports success while doing nothing at all,
        which is exactly what happened on the first live deploy: the only trace
        was a "coroutine ... was never awaited" RuntimeWarning.
        """

        async def handle(call: ServiceCall) -> None:
            try:
                await method(call.data[ATTR_LOAD])
            except CrestronError as err:
                # Surface refusals and confirmation failures to whoever called
                # the service instead of burying them in the log: a light that
                # did not change is exactly what an automation needs to know.
                raise HomeAssistantError(str(err)) from err

        return handle

    hass.services.async_register(DOMAIN, "turn_on", _service(bridge.async_turn_on), _SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, "turn_off", _service(bridge.async_turn_off), _SERVICE_SCHEMA
    )
    hass.services.async_register(DOMAIN, "toggle", _service(bridge.async_toggle), _SERVICE_SCHEMA)

    async def handle_enter_subsystem(call: ServiceCall) -> dict[str, object]:
        """Switch one slot between the Lights and A/V subsystems.

        Returns where the slot ended up rather than only logging it, because the
        interesting answer to "did the switch work" is the state afterwards, and
        a service that reports success without saying that is the failure mode
        this whole design is built around avoiding.
        """
        try:
            return await bridge.async_enter_subsystem(
                call.data[ATTR_LINK],
                call.data[ATTR_SUBSYSTEM],
                call.data[ATTR_HOLD_SECONDS],
            )
        except CrestronError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(
        DOMAIN,
        "enter_subsystem",
        handle_enter_subsystem,
        _ENTER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    def _av(method, *fields):
        """Wrap an A/V bridge method as a response-returning service handler.

        Every one of these returns the zone's state afterwards rather than only
        succeeding, because only the cursor's zone is readable at all and a
        caller that just moved the cursor is the one caller guaranteed to be
        able to see the result.
        """

        async def handle(call: ServiceCall) -> dict[str, object]:
            try:
                return await method(*(call.data[field] for field in fields)) or {}
            except CrestronError as err:
                raise HomeAssistantError(str(err)) from err

        return handle

    for name, method, schema, fields in (
        ("av_status", bridge.av.async_status, _ZONE_SCHEMA, (ATTR_ZONE,)),
        (
            "av_select_source",
            bridge.av.async_select_source,
            _SOURCE_SCHEMA,
            (ATTR_ZONE, ATTR_SOURCE),
        ),
        ("av_set_volume", bridge.av.async_set_volume, _VOLUME_SCHEMA, (ATTR_ZONE, ATTR_VOLUME)),
        ("av_power_off", bridge.av.async_power_off, _ZONE_SCHEMA, (ATTR_ZONE,)),
        ("av_power_off_all", bridge.av.async_power_off_all, vol.Schema({}), ()),
        ("av_mute", bridge.av.async_set_mute, _MUTE_SCHEMA, (ATTR_ZONE, ATTR_MUTE)),
    ):
        hass.services.async_register(
            DOMAIN,
            name,
            _av(method, *fields),
            schema,
            supports_response=SupportsResponse.OPTIONAL,
        )

    hass.async_create_task(async_load_platform(hass, Platform.BINARY_SENSOR, DOMAIN, {}, config))

    async def _stop(_event) -> None:
        await bridge.async_stop()

    hass.bus.async_listen_once("homeassistant_stop", _stop)
    return True
