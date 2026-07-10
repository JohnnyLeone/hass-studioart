"""Binary sensors for Revox STUDIOART."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import parse_battery
from .const import DOMAIN
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: RevoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RevoxBatteryChargingSensor(coordinator),
            RevoxPairedBatteryChargingSensor(coordinator),
        ]
    )


class RevoxBatteryChargingSensor(RevoxEntity, BinarySensorEntity):
    """Whether the speaker's battery is charging (battery byte 254).

    This is where the app's "Charging" status lives: the battery sensor
    stays numeric (for graphs/statistics) and shows unknown while charging,
    since the speaker reports no SoC then.
    """

    _attr_translation_key = "battery_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_battery_charging"

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        return st.battery_charging if st else None


class RevoxPairedBatteryChargingSensor(RevoxEntity, BinarySensorEntity):
    """Whether the paired client speaker's battery is charging."""

    _attr_translation_key = "paired_battery_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_paired_battery_charging"

    @property
    def available(self) -> bool:
        st = self.coordinator.data
        return super().available and bool(st and st.paired)

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        if st is None or not st.paired:
            return None
        _soc, charging = parse_battery(st.paired[0].get("battery"))
        return charging
