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

    The serial is read from the device at press time (the ``ID`` field of the
    paired[] entry), never configured — so this works on any speaker without
    the user knowing or typing a serial number.

    A button takes no input, so it only handles the unambiguous case of exactly
    one bound partner. That is the only case seen in practice (the firmware
    limits a stereo pair to a single client), but if a speaker ever reports
    several partners the button goes unavailable and the
    ``revox_studioart.unpair_speaker`` service, which accepts an explicit
    ``serial``, is the way to pick one.
    """

    _attr_translation_key = "kleernet_unpair"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_kleernet_unpair"

    def _sole_partner(self) -> dict | None:
        """The single bound partner, or None if there are zero or several."""
        st = self.coordinator.data
        paired = [p for p in (st.paired if st else []) if p.get("ID")]
        return paired[0] if len(paired) == 1 else None

    @property
    def available(self) -> bool:
        return super().available and self._sole_partner() is not None

    @property
    def extra_state_attributes(self) -> dict:
        """Show which speaker this will unpair, so the button is not a mystery."""
        partner = self._sole_partner()
        if partner is None:
            return {}
        return {
            "target_name": partner.get("name"),
            "target_serial": partner.get("ID"),
        }

    async def async_press(self) -> None:
        partner = self._sole_partner()
        if partner is None:
            raise HomeAssistantError(
                "Expected exactly one paired partner speaker. Use the "
                "revox_studioart.unpair_speaker service with a 'serial' to "
                "choose which one to unpair."
            )
        await self.coordinator.client.kleernet_unpair(partner["ID"])
        # paired[] empties roughly four seconds later (packet-capture timed)
        await asyncio.sleep(5)
        await self.coordinator.async_request_refresh()
