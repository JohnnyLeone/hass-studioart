"""Buttons for Revox STUDIOART."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import RevoxConfigEntry, RevoxCoordinator
from .entity import RevoxEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevoxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            RevoxRestartButton(coordinator),
            RevoxCheckP100Button(coordinator),
            RevoxKleernetPairModeButton(coordinator),
            RevoxKleernetUnpairButton(coordinator),
        ]
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
    """ "Check P100" in the app: probe whether a wired P100 partner speaker
    is connected to the A100.

    Sends group 3 / 0x0F (confirmed on the wire, no reply). Independent of
    Kleernet pairing — the P100 is a wired passive speaker.
    """

    _attr_translation_key = "check_p100"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        # unique_id kept from the earlier "identify paired" incarnation so
        # the registry entry (and history) survives the rename
        self._attr_unique_id = f"{self._unique_base}_identify_paired"

    async def async_press(self) -> None:
        await self.coordinator.client.check_p100()


class RevoxKleernetPairModeButton(RevoxEntity, ButtonEntity):
    """Put the speaker into Kleernet pairing mode (group 3 / 0x01).

    The StudioART app sends this after unpairing; the partner reappeared about
    eleven seconds later with no further network traffic, so the bind itself
    runs over the Kleernet radio. The partner speaker may also need putting
    into pairing mode for it to complete.
    """

    _attr_translation_key = "kleernet_pair_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_kleernet_pair_mode"

    async def async_press(self) -> None:
        await self.coordinator.client.kleernet_pair_mode()
        await self.coordinator.async_request_refresh()


class RevoxKleernetUnpairButton(RevoxEntity, ButtonEntity):
    """Unpair the bound Kleernet partner speaker (group 3 / 0x05 + serial).

    Only available while a partner is actually bound, since the command needs
    that partner's serial number. The speaker takes a few seconds to drop it,
    so the state is re-read after a short delay.
    """

    _attr_translation_key = "kleernet_unpair"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_kleernet_unpair"

    @property
    def available(self) -> bool:
        st = self.coordinator.data
        return super().available and bool(st and st.kleernet_partner_serial)

    async def async_press(self) -> None:
        st = self.coordinator.data
        serial = st.kleernet_partner_serial if st else None
        if not serial:
            raise HomeAssistantError("no Kleernet partner speaker is paired")
        await self.coordinator.client.kleernet_unpair(serial)
        # paired[] empties roughly four seconds later (packet-capture timed)
        await asyncio.sleep(5)
        await self.coordinator.async_request_refresh()
