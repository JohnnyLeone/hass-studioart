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
        [RevoxRestartButton(coordinator), RevoxCheckP100Button(coordinator)]
    )


class RevoxRestartButton(RevoxEntity, ButtonEntity):
    """Reboot the speaker (power action group 2 / 0x4D, value 2).

    Confirmed on the wire: the speaker acks with {"poweroff":1} and reboots
    (it drops off the network for a short while).
    """

    # explicit name: the device-class name would be localized by HA, while
    # all other entities carry the app's original (English) labels
    _attr_translation_key = "restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_restart"

    async def async_press(self) -> None:
        await self.coordinator.client.restart()


class RevoxCheckP100Button(RevoxEntity, ButtonEntity):
    """"Check P100" in the app: probe whether a wired P100 partner speaker
    is connected to the A100.

    Sends group 3 / 0x0F (confirmed on the wire, no reply). Independent of
    Kleernet pairing — the P100 is a wired passive speaker.
    """

    _attr_translation_key = "check_p100"
    _attr_icon = "mdi:speaker"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        # unique_id kept from the earlier "identify paired" incarnation so
        # the registry entry (and history) survives the rename
        self._attr_unique_id = f"{self._unique_base}_identify_paired"

    async def async_press(self) -> None:
        await self.coordinator.client.check_p100()
