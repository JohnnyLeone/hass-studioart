"""Buttons for Revox STUDIOART."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: RevoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [RevoxRestartButton(coordinator), RevoxIdentifyPairedButton(coordinator)]
    )


class RevoxRestartButton(RevoxEntity, ButtonEntity):
    """Reboot the speaker (power action group 2 / 0x4D, value 2).

    Confirmed on the wire: the speaker acks with {"poweroff":1} and reboots
    (it drops off the network for a short while).
    """

    # the restart device class provides the entity name
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_restart"

    async def async_press(self) -> None:
        await self.coordinator.client.restart()


class RevoxIdentifyPairedButton(RevoxEntity, ButtonEntity):
    """"Check P100" in the app: the paired speaker identifies itself.

    Sends group 3 / 0x0F (confirmed on the wire, no reply). Only available
    while a client speaker is paired.
    """

    _attr_translation_key = "identify_paired"
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_identify_paired"

    @property
    def available(self) -> bool:
        st = self.coordinator.data
        return super().available and bool(st and st.paired)

    async def async_press(self) -> None:
        await self.coordinator.client.identify_paired_speaker()
