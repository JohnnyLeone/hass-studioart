"""Max-volume limit control for Revox STUDIOART."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
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
    async_add_entities([RevoxMaxVolume(coordinator)])


class RevoxMaxVolume(RevoxEntity, RestoreNumber):
    """Volume limit via `cmd maxvolume N`.

    The speaker does not report the limit back, so the entity is optimistic
    and restores its last known value across Home Assistant restarts.
    """

    _attr_translation_key = "max_volume_limit"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_maxvolume"
        self._value: float = 100

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        data = await self.async_get_last_number_data()
        if data is not None and data.native_value is not None:
            self._value = data.native_value

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.client.set_max_volume(int(value))
        self._value = value
        self.async_write_ha_state()
