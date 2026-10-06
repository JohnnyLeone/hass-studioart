"""Diagnostic sensors for Revox STUDIOART."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.icon import icon_for_battery_level
from homeassistant.helpers.typing import StateType

from .api import RevoxState, parse_battery
from .coordinator import RevoxConfigEntry, RevoxCoordinator
from .entity import RevoxEntity, RevoxPartnerEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RevoxSensorDescription(SensorEntityDescription):
    value: Callable[[RevoxState], StateType]


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
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda st: WIFI_QUALITY.get(st.rssi),
    ),
    RevoxSensorDescription(
        key="ip",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda st: st.ip,
    ),
    RevoxSensorDescription(
        # LED ring brightness as reported by the device (0-100).
        key="brightness",
        translation_key="brightness",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda st: st.brightness,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevoxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [RevoxSensor(coordinator, desc) for desc in SENSORS]
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

    def __init__(
        self, coordinator: RevoxCoordinator, desc: RevoxSensorDescription
    ) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> StateType:
        st = self.coordinator.data
        return None if st is None else self.entity_description.value(st)


class RevoxBatteryBase(RevoxEntity, RestoreSensor):
    """Shared behaviour for the battery sensors.

    Numeric with statistics. The speaker reports no SoC while charging, so
    the sensor holds the last known percentage (kept across restarts) — the
    charging bolt icon and the ``charging`` attribute mark it as the level
    from before charging started.
    """

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._last_soc: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        data = await self.async_get_last_sensor_data()
        # older versions could have stored a textual state — numbers only
        if data is not None and isinstance(data.native_value, (int, float)):
            self._last_soc = round(data.native_value)
        self._update_from_data()

    def _reading(self, st: RevoxState) -> tuple[int | None, bool | None]:
        """Return the (SoC, charging) reading this sensor reports."""
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_data()
        super()._handle_coordinator_update()

    def _update_from_data(self) -> None:
        st = self.coordinator.data
        soc, charging = (None, None) if st is None else self._reading(st)
        if soc is not None:
            self._last_soc = soc
        # while charging no SoC is reported: hold the level from before
        held = charging and soc is None and self._last_soc is not None
        self._attr_native_value = self._last_soc if held else soc
        self._attr_icon = icon_for_battery_level(
            soc if soc is not None else self._last_soc, bool(charging)
        )
        self._attr_extra_state_attributes = {
            "charging": charging,
            # flags that the shown value is held from before charging began
            "soc_is_last_known": held,
        }


class RevoxBatterySensor(RevoxBatteryBase):
    """Battery state of charge of the speaker itself."""

    _attr_translation_key = "battery"

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "battery")

    def _reading(self, st: RevoxState) -> tuple[int | None, bool | None]:
        return st.battery, st.battery_charging


class RevoxPairedSpeakerSensor(RevoxPartnerEntity, SensorEntity):
    """Name and details of the paired client speaker (e.g. the stereo partner)."""

    _attr_translation_key = "paired_speaker"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "paired_speaker")

    @property
    def native_value(self) -> str | None:
        partner = self.partner
        return partner.get("name") if partner else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        partner = self.partner
        if not partner:
            return {}
        paired = self.coordinator.data.paired
        return {
            "serial": partner.get("ID"),
            "type": partner.get("type"),
            "volume": partner.get("volume"),
            "channel": partner.get("channel"),
            "paired_count": len(paired),
            "all_paired": [p.get("name") for p in paired],
        }


class RevoxPairedBatterySensor(RevoxPartnerEntity, RevoxBatteryBase):
    """Battery SoC of the paired client speaker (see RevoxBatteryBase)."""

    _attr_translation_key = "paired_speaker_battery"

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "paired_battery")

    def _reading(self, st: RevoxState) -> tuple[int | None, bool | None]:
        # same encoding as the chief: 254 = charging (SoC unknown), 255 = full
        partner = st.primary_partner
        return parse_battery(partner.get("battery") if partner else None)
