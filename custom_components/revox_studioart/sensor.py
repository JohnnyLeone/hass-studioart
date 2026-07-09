"""Diagnostic sensors for Revox STUDIOART."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RevoxState
from .const import DOMAIN
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity


@dataclass(frozen=True, kw_only=True)
class RevoxSensorDescription(SensorEntityDescription):
    value: Callable[[RevoxState], object]


# Wi-Fi quality code as shown by the app's "Signal Quality" field
# (higher = worse; 1 = best, inferred).
WIFI_QUALITY: dict[int, str] = {
    1: "Very good",
    2: "Good",
    3: "Bad",
    4: "Very bad",
}


SENSORS: tuple[RevoxSensorDescription, ...] = (
    RevoxSensorDescription(
        # the raw value 255 ("full / on mains") is normalized to 100 in api.py
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda st: st.battery,
    ),
    RevoxSensorDescription(
        key="wifi_ssid",
        name="Wi-Fi SSID",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda st: st.ssid,
    ),
    RevoxSensorDescription(
        # The speaker reports a quality code, higher = worse. 2-4 were
        # observed against the app's "Signal Quality" label; 1 is inferred.
        key="wifi_rssi",
        name="Wi-Fi signal quality",
        device_class=SensorDeviceClass.ENUM,
        options=list(WIFI_QUALITY.values()),
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda st: WIFI_QUALITY.get(st.rssi),
    ),
    RevoxSensorDescription(
        key="ip",
        name="IP address",
        icon="mdi:ip-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda st: st.ip,
    ),
    RevoxSensorDescription(
        # LED ring brightness as reported by the device (0-100).
        key="brightness",
        name="Display brightness",
        icon="mdi:brightness-6",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda st: st.brightness,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: RevoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(RevoxSensor(coordinator, desc) for desc in SENSORS)


class RevoxSensor(RevoxEntity, SensorEntity):
    entity_description: RevoxSensorDescription

    def __init__(self, coordinator: RevoxCoordinator, desc: RevoxSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._attr_unique_id = f"{self._unique_base}_{desc.key}"

    @property
    def native_value(self):
        st = self.coordinator.data
        if st is None:
            return None
        return self.entity_description.value(st)
