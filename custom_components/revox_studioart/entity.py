"""Shared base entities for Revox STUDIOART."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER
from .coordinator import RevoxCoordinator


class RevoxEntity(CoordinatorEntity[RevoxCoordinator]):
    """Base class wiring unique id and device info from the coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RevoxCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_base}_{key}"
        self._attr_device_info = _device_info(coordinator)


class RevoxPartnerEntity(RevoxEntity):
    """Base for entities describing the paired Kleernet client speaker.

    Unavailable while no partner is bound, rather than showing "unknown".
    """

    @property
    def partner(self) -> dict[str, Any] | None:
        st = self.coordinator.data
        return st.primary_partner if st else None

    @property
    def available(self) -> bool:
        return super().available and self.partner is not None


def _device_info(coordinator: RevoxCoordinator) -> DeviceInfo:
    host = coordinator.config_entry.data[CONF_HOST]
    st = coordinator.data
    connections = set()
    if st and st.mac:
        connections.add((CONNECTION_NETWORK_MAC, st.mac.lower()))
    # one unified firmware string ("V3957 / Controller V44"); the parts are
    # the LS9 main firmware and the controller version from the device status
    sw_version = None
    if st and st.firmware_ls9:
        sw_version = st.firmware_ls9
        if st.firmware_controller:
            sw_version = f"{st.firmware_ls9} / Controller {st.firmware_controller}"
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.unique_base)},
        connections=connections,
        manufacturer=MANUFACTURER,
        model="STUDIOART A100",
        name=(st.name if st else None) or DEFAULT_NAME,
        serial_number=st.serial if st else None,
        sw_version=sw_version,
        configuration_url=f"http://{host}",
    )
