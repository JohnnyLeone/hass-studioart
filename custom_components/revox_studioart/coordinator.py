"""DataUpdateCoordinator for the Revox STUDIOART integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RevoxError, RevoxState, RevoxStudioArtClient, merge_state
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type RevoxConfigEntry = ConfigEntry[RevoxCoordinator]


def firmware_version(state: RevoxState | None) -> str | None:
    """One firmware string for the device card: "V3957 / Controller V44".

    The parts are the LS9 main firmware and the controller version from the
    device status.
    """
    if state is None or not state.firmware_ls9:
        return None
    if state.firmware_controller:
        return f"{state.firmware_ls9} / Controller {state.firmware_controller}"
    return state.firmware_ls9


class RevoxCoordinator(DataUpdateCoordinator[RevoxState]):
    """Polls the speaker and applies push updates from the event channel."""

    config_entry: RevoxConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: RevoxConfigEntry, client: RevoxStudioArtClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            # pushes trigger confirming polls; keep them snappy but coalesced
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=1.5, immediate=True
            ),
        )
        self.client = client
        # entities are created after the first successful refresh, so the
        # serial is normally known by then; the host is the legacy fallback
        self.unique_base: str = entry.data[CONF_HOST]
        self._burst_task: asyncio.Task | None = None
        self._settle_task: asyncio.Task | None = None

    async def _async_update_data(self) -> RevoxState:
        try:
            state = await self.client.async_get_state()
        except RevoxError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if self.data is None and state.serial:
            self.unique_base = state.serial
        elif self.data is not None:
            self._sync_device_registry(state)
        return state

    def _sync_device_registry(self, state: RevoxState) -> None:
        """Carry firmware and name changes onto the existing device.

        Entities hand Home Assistant their device info only when they are
        added, so without this a firmware update or a rename in the app would
        show on the device card only after a restart.
        """
        old = self.data
        sw_version = firmware_version(state)
        changes: dict[str, str] = {}
        if sw_version and sw_version != firmware_version(old):
            changes["sw_version"] = sw_version
        if state.name and state.name != old.name:
            changes["name"] = state.name
        if not changes:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.unique_base)})
        if device is not None:
            registry.async_update_device(device.id, **changes)

    async def async_command(
        self, coro: Coroutine[Any, Any, Any], *, settle: float = 0.0
    ) -> None:
        """Run a control coroutine then refresh state quickly.

        ``settle`` schedules one extra refresh that many seconds later, for
        commands the speaker applies asynchronously — unpairing takes about
        four seconds to show up in ``paired[]``. It is scheduled rather than
        awaited so the caller (a button press, a service call) returns at once.

        Communication failures surface as ``HomeAssistantError`` so the UI
        shows a readable message instead of an unhandled exception.
        """
        try:
            await coro
        except RevoxError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"host": self.client.host, "error": str(err)},
            ) from err
        await self.async_request_refresh()
        if settle > 0:
            if self._settle_task is not None:
                self._settle_task.cancel()
            self._settle_task = self._background(
                self._async_settle_refresh(settle), "settle refresh"
            )

    def _background(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        """Run ``coro`` as a task that is cancelled when the entry unloads."""
        return self.config_entry.async_create_background_task(
            self.hass, coro, f"{self.name} {name}"
        )

    async def _async_settle_refresh(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.async_request_refresh()

    # -- push updates --------------------------------------------------------
    def start_events(self) -> None:
        """Subscribe to the speaker's push channel (port 7777)."""
        self.client.start_events(self._handle_push)

    async def async_stop_events(self) -> None:
        for task in (self._burst_task, self._settle_task):
            if task is not None:
                task.cancel()
        self._burst_task = self._settle_task = None
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
        if partial.get("_activity"):
            # a decoded push carries only part of the picture (e.g. the
            # source id but not the play state) — always confirm with a
            # debounced poll shortly after
            self._background(self.async_request_refresh(), "push refresh")
            # ...and again a little later: on stream start the playback
            # JSON flips to "playing" only ~2-3 s after the push markers
            if self._burst_task is None or self._burst_task.done():
                self._burst_task = self._background(
                    self._async_burst_refresh(), "burst refresh"
                )

    async def _async_burst_refresh(self) -> None:
        for _ in range(2):
            await asyncio.sleep(2.0)
            await self.async_request_refresh()
