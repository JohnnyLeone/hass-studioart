"""Select entities for Revox STUDIOART."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RevoxState, RevoxStudioArtClient
from .const import (
    CHANNEL_COMMANDS,
    CHANNEL_OPTIONS,
    CHANNEL_TOKEN_TO_OPTION,
    KLEERNET_BAND_OPTIONS,
    POWER_ON_SOURCE_OPTIONS,
)
from .coordinator import RevoxConfigEntry, RevoxCoordinator
from .entity import RevoxEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RevoxSelectDescription(SelectEntityDescription):
    """A select backed by a numeric device setting."""

    labels: dict[int, str]  # wire value -> option label
    value: Callable[[RevoxState], int | None]
    set_fn: Callable[[RevoxStudioArtClient, int], Awaitable[None]]


SELECTS: tuple[RevoxSelectDescription, ...] = (
    # Default source after manual power on (device field "PowerOnSrc").
    # Set = group 2 / 0x58 (ack {"PowerOnSrc":n}); every index was confirmed
    # on the wire by cycling the app's menu.
    RevoxSelectDescription(
        key="poweronsrc",
        translation_key="power_on_source",
        entity_category=EntityCategory.CONFIG,
        labels=POWER_ON_SOURCE_OPTIONS,
        value=lambda st: st.power_on_source,
        set_fn=lambda client, idx: client.set_power_on_source(idx),
    ),
    # Kleernet wireless band between chief and client speakers. Set = group 2
    # / 0x9B (values confirmed on a live speaker); state is the "D83Fre"
    # field of the Kleernet JSON (group 3 / 0x57).
    RevoxSelectDescription(
        key="kleernet_band",
        translation_key="kleernet_band",
        entity_category=EntityCategory.CONFIG,
        labels=KLEERNET_BAND_OPTIONS,
        value=lambda st: st.kleernet_band,
        set_fn=lambda client, band: client.set_kleernet_band(band),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevoxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [RevoxChannelSelect(coordinator)]
    entities.extend(RevoxSettingSelect(coordinator, desc) for desc in SELECTS)
    async_add_entities(entities)


class RevoxSettingSelect(RevoxEntity, SelectEntity):
    """A numeric device setting presented by its app label."""

    entity_description: RevoxSelectDescription

    def __init__(
        self, coordinator: RevoxCoordinator, desc: RevoxSelectDescription
    ) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc
        self._attr_options = list(desc.labels.values())
        self._label_to_value = {label: v for v, label in desc.labels.items()}

    @property
    def current_option(self) -> str | None:
        st = self.coordinator.data
        value = None if st is None else self.entity_description.value(st)
        return None if value is None else self.entity_description.labels.get(value)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_command(
            self.entity_description.set_fn(
                self.coordinator.client, self._label_to_value[option]
            )
        )


class RevoxChannelSelect(RevoxEntity, SelectEntity):
    """ "Multi-room Speaker Setting" in the app: Stereo / Left / Right.

    SETSTEREO / SETLEFT / SETRIGHT are sent over the event channel (op 0x6A)
    exactly like the official app. The speaker confirms with an 0x67 status
    push ("FREE,STEREO,..."), which the coordinator folds into the state — so
    the shown option is device-reported, with the last command as fallback
    until the first push arrives.
    """

    _attr_translation_key = "multiroom_channel"
    _attr_options = CHANNEL_OPTIONS

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "channel")
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
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.coordinator.data
        # ddms_state is the Wi-Fi multi-room state; Kleernet pairing is
        # reported separately by the "Partner speaker paired" binary sensor.
        return {"ddms_state": st.ddms_state if st else None}

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_command(
            self.coordinator.client.set_channel(CHANNEL_COMMANDS[option])
        )
        self._optimistic = option
        self.async_write_ha_state()
