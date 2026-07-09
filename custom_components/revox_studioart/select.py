"""Select entities for Revox STUDIOART."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHANNEL_COMMANDS,
    CHANNEL_OPTIONS,
    CHANNEL_TOKEN_TO_OPTION,
    DOMAIN,
    KLEERNET_BAND_OPTIONS,
    POWER_ON_SOURCE_OPTIONS,
)
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: RevoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RevoxChannelSelect(coordinator),
            RevoxPowerOnSourceSelect(coordinator),
            RevoxKleernetBandSelect(coordinator),
        ]
    )


class RevoxChannelSelect(RevoxEntity, SelectEntity):
    """Stereo / Left / Right channel assignment.

    SETSTEREO / SETLEFT / SETRIGHT are sent over the event channel (op 0x6A)
    exactly like the official app. The speaker confirms with an 0x67 status
    push ("FREE,STEREO,..."), which the coordinator folds into the state — so
    the shown option is device-reported, with the last command as fallback
    until the first push arrives.
    """

    _attr_name = "Channel"
    _attr_icon = "mdi:speaker-multiple"
    _attr_options = CHANNEL_OPTIONS

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_channel"
        self._optimistic: str | None = None

    @property
    def current_option(self) -> str | None:
        st = self.coordinator.data
        if st is not None and st.channel:
            option = CHANNEL_TOKEN_TO_OPTION.get(st.channel.upper())
            if option:
                return option
        return self._optimistic

    @property
    def extra_state_attributes(self) -> dict:
        st = self.coordinator.data
        return {"pair_state": st.pair_state if st else None}

    async def async_select_option(self, option: str) -> None:
        cmd = CHANNEL_COMMANDS.get(option)
        if not cmd:
            return
        self._optimistic = option
        await self.coordinator.client.set_channel(cmd)
        self.async_write_ha_state()


class RevoxPowerOnSourceSelect(RevoxEntity, SelectEntity):
    """Default source after manual power on (device field "PowerOnSrc").

    Set = group 2 / 0x58 (ack {"PowerOnSrc":n}); every index was confirmed on
    the wire by cycling the app's menu.
    """

    _attr_name = "Power-on source"
    _attr_icon = "mdi:power-on"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(POWER_ON_SOURCE_OPTIONS.values())

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_poweronsrc"

    @property
    def current_option(self) -> str | None:
        st = self.coordinator.data
        if st is None or st.power_on_source is None:
            return None
        return POWER_ON_SOURCE_OPTIONS.get(st.power_on_source)

    async def async_select_option(self, option: str) -> None:
        for index, label in POWER_ON_SOURCE_OPTIONS.items():
            if label == option:
                await self.coordinator.async_command(
                    self.coordinator.client.set_power_on_source(index)
                )
                return


class RevoxKleernetBandSelect(RevoxEntity, SelectEntity):
    """Kleernet wireless band between chief and client speakers.

    Set = group 2 / 0x9B (values confirmed on a live speaker); state is the
    "D83Fre" field of the Kleernet JSON (group 3 / 0x57).
    """

    _attr_name = "Kleernet wireless band"
    _attr_icon = "mdi:radio-tower"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(KLEERNET_BAND_OPTIONS.values())

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_kleernet_band"

    @property
    def current_option(self) -> str | None:
        st = self.coordinator.data
        if st is None or st.kleernet_band is None:
            return None
        return KLEERNET_BAND_OPTIONS.get(st.kleernet_band)

    async def async_select_option(self, option: str) -> None:
        for band, label in KLEERNET_BAND_OPTIONS.items():
            if label == option:
                await self.coordinator.async_command(
                    self.coordinator.client.set_kleernet_band(band)
                )
                return
