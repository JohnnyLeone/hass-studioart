"""The Revox STUDIOART integration."""

from __future__ import annotations

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.typing import ConfigType

from .api import RevoxStudioArtClient
from .const import DEFAULT_PORT, DOMAIN
from .coordinator import RevoxConfigEntry, RevoxCoordinator

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SEND_COMMAND = "send_command"

_SEND_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Optional("ascii"): cv.string,  # e.g. "volume 40" -> sends `cmd volume 40`
        vol.Optional("raw"): cv.string,  # e.g. "SETLEFT" -> sends "SETLEFT\r\n"
        vol.Optional("bin_group"): vol.All(int, vol.Range(min=0, max=65535)),
        vol.Optional("bin_cmd"): vol.All(int, vol.Range(min=0, max=255)),
        vol.Optional("bin_value"): vol.All(int, vol.Range(min=0, max=255)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services."""

    async def _handle_send(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data["entry_id"])
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
    return True


def _resolve_coordinator(hass: HomeAssistant, entry_id: str) -> RevoxCoordinator:
    """Return the coordinator for ``entry_id``, or the first loaded speaker."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        # fall back to the first configured device (pre-0.10 behaviour)
        entry = next(iter(hass.config_entries.async_loaded_entries(DOMAIN)), None)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("No loaded Revox STUDIOART device found")
    return entry.runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: RevoxConfigEntry) -> bool:
    """Set up Revox STUDIOART from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = RevoxStudioArtClient(host, port)
    coordinator = RevoxCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # live push updates from the speaker's event channel (port 7777)
    coordinator.start_events()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RevoxConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop_events()
    return unload_ok
