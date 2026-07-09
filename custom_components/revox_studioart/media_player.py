"""Media player for Revox STUDIOART."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SOURCE_COMMANDS
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity

# Best-effort mapping of the integer reported in the playback status back to a
# source name. Verified indices can be filled in over time; unknown values fall
# back to the last source we selected.
SOURCE_INT_TO_NAME: dict[int, str] = {}

SUPPORT = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY_MEDIA
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: RevoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RevoxMediaPlayer(coordinator)])


class RevoxMediaPlayer(RevoxEntity, MediaPlayerEntity):
    """A STUDIOART speaker as a media player."""

    _attr_name = None  # use the device name
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = SUPPORT
    _attr_source_list = list(SOURCE_COMMANDS)

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_media_player"
        self._last_source: str | None = None
        self._volume_before_mute: int | None = None

    @property
    def state(self) -> MediaPlayerState:
        st = self.coordinator.data
        if st is None or not st.available:
            return MediaPlayerState.OFF
        if st.standby:
            return MediaPlayerState.OFF
        # play_state codes are not fully documented; treat 0 as idle.
        if st.play_state and st.play_state != 0:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        st = self.coordinator.data
        if st is None or st.volume is None:
            return None
        return max(0.0, min(1.0, st.volume / 100))

    @property
    def is_volume_muted(self) -> bool | None:
        st = self.coordinator.data
        if st is None or st.volume is None:
            return None
        return st.volume == 0

    @property
    def source(self) -> str | None:
        st = self.coordinator.data
        if st is not None and st.source in SOURCE_INT_TO_NAME:
            return SOURCE_INT_TO_NAME[st.source]
        return self._last_source

    @property
    def extra_state_attributes(self) -> dict:
        st = self.coordinator.data
        if st is None:
            return {}
        return {
            "battery": st.battery,
            "wifi_ssid": st.ssid,
            "wifi_rssi": st.rssi,
            "paired_speakers": [p.get("name") for p in st.paired],
            "paired_details": st.paired or None,
            "multiroom_channel": st.channel,
            "pair_state": st.pair_state,
            "lr_reverse": st.lr_reverse,
            "raw_source_index": st.source,
        }

    # -- commands ----------------------------------------------------------
    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.async_command(
            self.coordinator.client.set_volume(round(volume * 100))
        )

    async def async_volume_up(self) -> None:
        await self.coordinator.async_command(self.coordinator.client.volume_up())

    async def async_volume_down(self) -> None:
        await self.coordinator.async_command(self.coordinator.client.volume_down())

    async def async_mute_volume(self, mute: bool) -> None:
        st = self.coordinator.data
        if mute:
            if st and st.volume:
                self._volume_before_mute = st.volume
            await self.coordinator.async_command(self.coordinator.client.set_volume(0))
        else:
            restore = self._volume_before_mute or 20
            await self.coordinator.async_command(
                self.coordinator.client.set_volume(restore)
            )

    async def async_select_source(self, source: str) -> None:
        cmd = SOURCE_COMMANDS.get(source)
        if not cmd:
            return
        self._last_source = source
        await self.coordinator.async_command(
            self.coordinator.client.select_source(cmd)
        )

    async def async_media_play(self) -> None:
        await self.coordinator.async_command(self.coordinator.client.play())

    async def async_media_pause(self) -> None:
        await self.coordinator.async_command(self.coordinator.client.pause())

    async def async_turn_off(self) -> None:
        await self.coordinator.async_command(self.coordinator.client.standby())

    async def async_turn_on(self) -> None:
        # No dedicated "power on" command exists; starting playback wakes the
        # speaker from standby.
        await self.coordinator.async_command(self.coordinator.client.play())

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        if media_type in (MediaType.URL, MediaType.MUSIC, "url", "audio/mp3"):
            await self.coordinator.async_command(
                self.coordinator.client.play_url(media_id)
            )
