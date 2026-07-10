"""The Revox STUDIOART integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .api import RevoxStudioArtClient
from .const import DEFAULT_PORT, DOMAIN
from .coordinator import RevoxCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Revox STUDIOART from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = RevoxStudioArtClient(host, port)
    coordinator = RevoxCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # live push updates from the speaker's event channel (port 7777)
    coordinator.start_events()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


SERVICE_SEND_COMMAND = "send_command"

_SEND_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Optional("ascii"): cv.string,      # e.g. "volume 40"  -> sends `cmd volume 40`
        vol.Optional("raw"): cv.string,        # e.g. "SETLEFT"     -> sends "SETLEFT\r\n"
        vol.Optional("bin_group"): vol.All(int, vol.Range(min=0, max=65535)),
        vol.Optional("bin_cmd"): vol.All(int, vol.Range(min=0, max=255)),
        vol.Optional("bin_value"): vol.All(int, vol.Range(min=0, max=255)),
    }
)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        return

    async def _handle_send(call: ServiceCall) -> None:
        coordinator: RevoxCoordinator | None = hass.data.get(DOMAIN, {}).get(
            call.data["entry_id"]
        )
        if coordinator is None:
            # fall back to the first configured device
            entries = hass.data.get(DOMAIN, {})
            coordinator = next(iter(entries.values()), None)
        if coordinator is None:
            return
        client = coordinator.client
        if "ascii" in call.data:
            await client.async_send_cmd(call.data["ascii"])
        if "raw" in call.data:
            await client.async_send_raw_ascii(call.data["raw"])
        if {"bin_group", "bin_cmd", "bin_value"} <= set(call.data):
            await client.async_set_bin(
                call.data["bin_group"], call.data["bin_cmd"], call.data["bin_value"]
            )
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, _handle_send, schema=_SEND_SCHEMA
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: RevoxCoordinator | None = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_stop_events()
    return unload_ok
