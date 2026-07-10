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
from homeassistant.helpers.icon import icon_for_battery_level

from .api import RevoxState, parse_battery
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
        key="wifi_ssid",
        translation_key="wifi_ssid",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda st: st.ssid,
    ),
    RevoxSensorDescription(
        # The speaker reports a quality code, higher = worse. 2-4 were
        # observed against the app's "Signal Quality" label; 1 is inferred.
        key="wifi_rssi",
        translation_key="wifi_signal_quality",
        device_class=SensorDeviceClass.ENUM,
        options=list(WIFI_QUALITY.values()),
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda st: WIFI_QUALITY.get(st.rssi),
    ),
    RevoxSensorDescription(
        key="ip",
        translation_key="ip_address",
        icon="mdi:ip-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda st: st.ip,
    ),
    RevoxSensorDescription(
        # LED ring brightness as reported by the device (0-100).
        key="brightness",
        translation_key="brightness",
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
    entities: list[SensorEntity] = [
        RevoxSensor(coordinator, desc) for desc in SENSORS
    ]
    entities.extend(
        [
            RevoxBatterySensor(coordinator),
            RevoxPairedSpeakerSensor(coordinator),
            RevoxPairedBatterySensor(coordinator),
        ]
    )
    async_add_entities(entities)


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


class RevoxBatterySensor(RevoxEntity, SensorEntity):
    """Battery like the app shows it: the SoC, or "Charging".

    Deliberately no battery device class or % unit: the speaker does not
    report a state of charge while charging, and HA only allows the textual
    "Charging" state on a unit-less sensor. The trade-off is no long-term
    statistics for this sensor.
    """

    _attr_translation_key = "battery"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_battery"

    @property
    def _battery(self) -> tuple[int | None, bool | None]:
        st = self.coordinator.data
        if st is None:
            return None, None
        return st.battery, st.battery_charging

    @property
    def native_value(self) -> int | str | None:
        soc, charging = self._battery
        if charging:
            return "Charging"
        return soc

    @property
    def icon(self) -> str:
        soc, charging = self._battery
        return icon_for_battery_level(soc, bool(charging))

    @property
    def extra_state_attributes(self) -> dict:
        _soc, charging = self._battery
        return {"charging": charging}


class RevoxPairedBase(RevoxEntity, SensorEntity):
    """Base for sensors describing the paired Kleernet client speaker."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def _paired(self) -> dict | None:
        st = self.coordinator.data
        if st is None or not st.paired:
            return None
        return st.paired[0]

    @property
    def available(self) -> bool:
        return super().available and self._paired is not None


class RevoxPairedSpeakerSensor(RevoxPairedBase):
    """Name and details of the paired client speaker (e.g. the stereo partner)."""

    _attr_translation_key = "paired_speaker"
    _attr_icon = "mdi:speaker-multiple"

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_paired_speaker"

    @property
    def native_value(self) -> str | None:
        paired = self._paired
        return paired.get("name") if paired else None

    @property
    def extra_state_attributes(self) -> dict:
        paired = self._paired
        if not paired:
            return {}
        st = self.coordinator.data
        return {
            "serial": paired.get("ID"),
            "type": paired.get("type"),
            "volume": paired.get("volume"),
            "channel": paired.get("channel"),
            "paired_count": len(st.paired) if st else None,
            "all_paired": [p.get("name") for p in (st.paired if st else [])],
        }


class RevoxPairedBatterySensor(RevoxPairedBase):
    """Battery of the paired client speaker — SoC, or "Charging" (see
    RevoxBatterySensor for why there is no battery device class)."""

    _attr_translation_key = "paired_speaker_battery"

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_paired_battery"

    @property
    def _battery(self) -> tuple[int | None, bool | None]:
        paired = self._paired
        if not paired:
            return None, None
        # same encoding as the chief: 254 = charging (SoC unknown), 255 = full
        return parse_battery(paired.get("battery"))

    @property
    def native_value(self) -> int | str | None:
        soc, charging = self._battery
        if charging:
            return "Charging"
        return soc

    @property
    def icon(self) -> str:
        soc, charging = self._battery
        return icon_for_battery_level(soc, bool(charging))

    @property
    def extra_state_attributes(self) -> dict:
        _soc, charging = self._battery
        return {"charging": charging}
