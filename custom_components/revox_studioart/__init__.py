"""The Revox STUDIOART integration."""

from __future__ import annotations

import asyncio

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
SERVICE_UNPAIR_SPEAKER = "unpair_speaker"

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

# Unpair a Kleernet partner. `serial` is optional: with a single partner bound
# (the normal case) it is looked up from the device, so nothing is hardcoded.
_UNPAIR_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Optional("serial"): cv.string,
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

    async def _handle_unpair(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data["entry_id"])
        state = coordinator.data
        bound = [p.get("ID") for p in (state.paired if state else [])]
        bound = [b for b in bound if b]

        serial = (call.data.get("serial") or "").strip().upper()
        if not serial:
            # default to the bound partner, but only when it is unambiguous
            if not bound:
                raise ServiceValidationError(
                    "No partner speaker is paired to this device"
                )
            if len(bound) > 1:
                raise ServiceValidationError(
                    "Several partner speakers are paired ("
                    + ", ".join(bound)
                    + "); pass 'serial' to choose one"
                )
            serial = bound[0]
        elif bound and serial not in bound:
            raise ServiceValidationError(
                f"{serial} is not paired to this device"
                + (" (paired: " + ", ".join(bound) + ")" if bound else "")
            )

        await coordinator.client.kleernet_unpair(serial)
        # the speaker takes ~4 s to drop the partner (packet-capture timed)
        await asyncio.sleep(5)
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, _handle_send, schema=_SEND_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNPAIR_SPEAKER, _handle_unpair, schema=_UNPAIR_SCHEMA
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
