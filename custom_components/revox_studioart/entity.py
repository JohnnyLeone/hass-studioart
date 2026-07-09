"""Shared base entity for Revox STUDIOART."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import RevoxCoordinator


class RevoxEntity(CoordinatorEntity[RevoxCoordinator]):
    """Base class wiring device info from the coordinator's state."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._host = coordinator.entry.data["host"]

    @property
    def _unique_base(self) -> str:
        st = self.coordinator.data
        return (st.serial if st and st.serial else None) or self._host

    @property
    def device_info(self) -> DeviceInfo:
        st = self.coordinator.data
        connections = set()
        if st and st.mac:
            connections.add(("mac", st.mac.lower()))
        # one unified firmware string ("V3957 / Controller V44"); the parts
        # are the LS9 main firmware and the controller version from the
        # device status JSON.
        sw_version = None
        if st and st.firmware_ls9:
            sw_version = st.firmware_ls9
            if st.firmware_controller:
                sw_version = f"{st.firmware_ls9} / Controller {st.firmware_controller}"
        return DeviceInfo(
            identifiers={(DOMAIN, self._unique_base)},
            connections=connections,
            manufacturer=MANUFACTURER,
            model="STUDIOART A100",
            name=(st.name if st and st.name else None) or "STUDIOART Speaker",
            sw_version=sw_version,
            configuration_url=f"http://{self._host}",
        )
