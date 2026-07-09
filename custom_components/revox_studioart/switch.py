"""Switches for Revox STUDIOART (DSP / behaviour toggles).

All toggles below are backed by binary set commands confirmed in the packet
capture of the StudioART app and partially verified on a live speaker (see
README for the triplet table):

  * Loudness                  get 0x34 / set 0x36        (device-verified)
  * Aux-In trigger            set 0x9E INVERTED, state = Kleernet "DisAutoAux"
                              (device-verified; inversion handled in the client)
  * Aux-In high sensitivity   get 0x41 / set 0x43        (device-verified)
  * Switch L/R channel        set 0x62 (state = multi-room "LRreverse")
  * Auto power on             set 0x5B (state = device "AutoPowerOn")

Bass boost keeps the documented ASCII command and stays optimistic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RevoxState, RevoxStudioArtClient
from .const import DOMAIN
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity


@dataclass(frozen=True, kw_only=True)
class RevoxSwitchDescription(SwitchEntityDescription):
    value: Callable[[RevoxState], bool | None]
    set_fn: Callable[[RevoxStudioArtClient, bool], Awaitable[None]]


SWITCHES: tuple[RevoxSwitchDescription, ...] = (
    RevoxSwitchDescription(
        key="aux_trigger",
        name="Aux-In trigger",
        icon="mdi:audio-input-stereo-minijack",
        entity_category=EntityCategory.CONFIG,
        value=lambda st: st.aux_trigger,
        set_fn=lambda client, on: client.set_aux_trigger(on),
    ),
    RevoxSwitchDescription(
        key="aux_high_sens",
        name="Aux-In trigger high sensitivity",
        icon="mdi:knob",
        entity_category=EntityCategory.CONFIG,
        value=lambda st: st.aux_high_sensitivity,
        set_fn=lambda client, on: client.set_aux_high_sensitivity(on),
    ),
    RevoxSwitchDescription(
        key="loudness",
        name="Loudness",
        icon="mdi:volume-vibrate",
        value=lambda st: st.loudness,
        set_fn=lambda client, on: client.set_loudness(on),
    ),
    RevoxSwitchDescription(
        key="lr_swap",
        name="Switch L/R channel",
        icon="mdi:swap-horizontal",
        entity_category=EntityCategory.CONFIG,
        value=lambda st: st.lr_reverse,
        set_fn=lambda client, on: client.set_lr_swap(on),
    ),
    RevoxSwitchDescription(
        key="autopoweron",
        name="Auto power on",
        icon="mdi:power-settings",
        entity_category=EntityCategory.CONFIG,
        value=lambda st: st.auto_power_on,
        set_fn=lambda client, on: client.set_auto_power_on(on),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: RevoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        RevoxSwitch(coordinator, desc) for desc in SWITCHES
    ]
    entities.append(RevoxBassBoostSwitch(coordinator))
    async_add_entities(entities)


class RevoxSwitch(RevoxEntity, SwitchEntity):
    """A toggle with confirmed set command and device-reported state."""

    entity_description: RevoxSwitchDescription

    def __init__(self, coordinator: RevoxCoordinator, desc: RevoxSwitchDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._attr_unique_id = f"{self._unique_base}_{desc.key}"
        # last commanded value, used while the device does not report state
        # (e.g. loudness before the first poll confirms it)
        self._optimistic: bool | None = None

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        reported = None if st is None else self.entity_description.value(st)
        if reported is not None:
            return reported
        return self._optimistic

    async def _set(self, on: bool) -> None:
        self._optimistic = on
        await self.coordinator.async_command(
            self.entity_description.set_fn(self.coordinator.client, on)
        )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)


class RevoxBassBoostSwitch(RevoxEntity, SwitchEntity):
    """Bass boost via `cmd basssboost 0/1`. Optimistic (state not reported)."""

    _attr_name = "Bass boost"
    _attr_icon = "mdi:speaker"

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_bassboost"
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.set_bass_boost(True)
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.set_bass_boost(False)
        self._state = False
        self.async_write_ha_state()
