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
from .const import DEFAULT_PORT, DOMAIN, UNPAIR_SETTLE_SECONDS
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

# all three bin_* fields or none
_BIN = "binary set"
_BIN_MSG = "bin_group, bin_cmd and bin_value must be given together"
_SEND_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("entry_id"): cv.string,
            vol.Optional("ascii"): cv.string,  # "volume 40" -> `cmd volume 40`
            vol.Optional("raw"): cv.string,  # "SETLEFT" -> "SETLEFT\r\n"
            vol.Inclusive("bin_group", _BIN, msg=_BIN_MSG): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=65535)
            ),
            vol.Inclusive("bin_cmd", _BIN, msg=_BIN_MSG): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=255)
            ),
            vol.Inclusive("bin_value", _BIN, msg=_BIN_MSG): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=255)
            ),
        }
    ),
    cv.has_at_least_one_key("ascii", "raw", "bin_group"),
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
        data = call.data
        if "ascii" in data:
            await coordinator.async_command(client.async_send_cmd(data["ascii"]))
        if "raw" in data:
            await coordinator.async_command(client.async_send_raw_ascii(data["raw"]))
        if "bin_group" in data:
            await coordinator.async_command(
                client.async_set_bin(
                    data["bin_group"], data["bin_cmd"], data["bin_value"]
                )
            )

    async def _handle_unpair(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data["entry_id"])
        state = coordinator.data
        bound = [p["ID"] for p in (state.kleernet_partners if state else [])]

        serial = (call.data.get("serial") or "").strip().upper()
        if not serial:
            # Default to the bound partner, but only when it is unambiguous:
            # kleernet_partner is None unless exactly one is paired.
            partner = state.kleernet_partner if state else None
            if partner is None:
                raise ServiceValidationError(
                    "Several partner speakers are paired "
                    f"({', '.join(bound)}); pass 'serial' to choose one"
                    if bound
                    else "No partner speaker is paired to this device"
                )
            serial = partner["ID"]
        elif bound and serial not in bound:
            raise ServiceValidationError(
                f"{serial} is not paired to this device (paired: {', '.join(bound)})"
            )

        await coordinator.async_command(
            coordinator.client.kleernet_unpair(serial), settle=UNPAIR_SETTLE_SECONDS
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, _handle_send, schema=_SEND_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNPAIR_SPEAKER, _handle_unpair, schema=_UNPAIR_SCHEMA
    )
    return True


def _resolve_coordinator(hass: HomeAssistant, entry_id: str) -> RevoxCoordinator:
    """Return the coordinator for ``entry_id``.

    An unknown id still resolves when exactly one speaker is loaded (the
    pre-0.10 behaviour, kept for old automations) — but never guesses between
    several, since a command such as unpair must not hit the wrong speaker.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        loaded = hass.config_entries.async_loaded_entries(DOMAIN)
        if len(loaded) != 1:
            raise ServiceValidationError(
                f"{entry_id!r} is not a Revox STUDIOART config entry"
            )
        entry = loaded[0]
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"Speaker {entry.title} is not loaded")
    return entry.runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: RevoxConfigEntry) -> bool:
    """Set up Revox STUDIOART from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = RevoxStudioArtClient(host, port)
    coordinator = RevoxCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # live push updates from the speaker's event channel (port 7777); the
    # unload hook also runs if platform setup below fails
    coordinator.start_events()
    entry.async_on_unload(coordinator.async_stop_events)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RevoxConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
