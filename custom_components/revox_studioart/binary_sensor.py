"""Binary sensors for Revox STUDIOART."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import parse_battery
from .coordinator import RevoxConfigEntry, RevoxCoordinator
from .entity import RevoxEntity, RevoxPartnerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevoxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            RevoxBatteryChargingSensor(coordinator),
            RevoxPairedBatteryChargingSensor(coordinator),
            RevoxKleernetPairedSensor(coordinator),
        ]
    )


class RevoxBatteryChargingSensor(RevoxEntity, BinarySensorEntity):
    """Whether the speaker's battery is charging (battery byte 254).

    This is where the app's "Charging" status lives: the battery sensor
    stays numeric (for graphs/statistics) and holds the last known SoC while
    charging, since the speaker reports none then.
    """

    _attr_translation_key = "battery_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "battery_charging")

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        return st.battery_charging if st else None


class RevoxPairedBatteryChargingSensor(RevoxPartnerEntity, BinarySensorEntity):
    """Whether the paired client speaker's battery is charging."""

    _attr_translation_key = "paired_battery_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "paired_battery_charging")

    @property
    def is_on(self) -> bool | None:
        partner = self.partner
        return parse_battery(partner.get("battery"))[1] if partner else None


class RevoxKleernetPairedSensor(RevoxEntity, BinarySensorEntity):
    """Whether a Kleernet partner speaker is bound.

    Derived from the paired[] array (group 3 / 0x03), which a full unpair /
    re-pair packet capture confirmed is the authoritative source. The event
    0x67 push reports the *DDMS* (Wi-Fi multi-room) state and stayed "FREE"
    across that entire cycle, so it must not be used for this.
    """

    _attr_translation_key = "kleernet_paired"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "kleernet_paired")

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        return None if st is None else st.kleernet_paired

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.coordinator.data
        if st is None:
            return {}
        partner = st.kleernet_partner or {}
        return {
            "partner_name": partner.get("name"),
            "partner_serial": partner.get("ID"),
            "partner_type": partner.get("type"),
            "partner_channel": partner.get("channel"),
            # channel 0 while a bind is still settling
            "pairing_in_progress": st.kleernet_pairing,
        }
