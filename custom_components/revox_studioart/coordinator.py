"""DataUpdateCoordinator for the Revox STUDIOART integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RevoxError, RevoxState, RevoxStudioArtClient, merge_state
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class RevoxCoordinator(DataUpdateCoordinator[RevoxState]):
    """Polls the speaker and applies push updates from the event channel."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: RevoxStudioArtClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data.get('host')}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> RevoxState:
        try:
            return await self.client.async_get_state()
        except RevoxError as err:
            raise UpdateFailed(str(err)) from err

    async def async_command(self, coro) -> None:
        """Run a control coroutine then refresh state quickly."""
        await coro
        await self.async_request_refresh()

    # -- push updates --------------------------------------------------------
    def start_events(self) -> None:
        """Subscribe to the speaker's push channel (port 7777)."""
        self.client.start_events(self._handle_push)

    async def async_stop_events(self) -> None:
        await self.client.stop_events()

    @callback
    def _handle_push(self, partial: dict[str, Any]) -> None:
        """Apply a partial state update pushed by the speaker.

        Called from the client's listener task (same event loop). The mirror
        channel tells us the new value of a setting the moment *any* client
        changes it; a debounced poll follows to pick up JSON-backed fields.
        """
        if self.data is not None:
            updated = merge_state(self.data, partial)
            if updated is not self.data:
                self.async_set_updated_data(updated)
                return
        if partial.get("_activity"):
            # something happened that we cannot decode fully; poll soon
            self.hass.async_create_task(self.async_request_refresh())
