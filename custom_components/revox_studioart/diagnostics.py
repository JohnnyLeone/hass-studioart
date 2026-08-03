"""Diagnostics support for Revox STUDIOART."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import RevoxConfigEntry

TO_REDACT = {
    # config entry
    "host",
    # state fields
    "name",
    "ip",
    "mac",
    "serial",
    "ssid",
    # raw device JSON
    "Name",
    "IP",
    "MAC",
    "SN",
    "SSID",
    # paired[] entries
    "ID",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RevoxConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "events_connected": coordinator.client.events_connected,
        "state": async_redact_data(asdict(coordinator.data), TO_REDACT)
        if coordinator.data is not None
        else None,
    }
