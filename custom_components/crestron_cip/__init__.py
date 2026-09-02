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
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .bridge import CrestronBridge, CrestronError
from .const import DOMAIN, LINK_AADS, LINK_MC2E, LOADS_BY_KEY

_LOGGER = logging.getLogger(__name__)

CONF_IPID = "ipid"
ATTR_LOAD = "load"

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

    hass.async_create_task(async_load_platform(hass, Platform.BINARY_SENSOR, DOMAIN, {}, config))

    async def _stop(_event) -> None:
        await bridge.async_stop()

    hass.bus.async_listen_once("homeassistant_stop", _stop)
    return True
