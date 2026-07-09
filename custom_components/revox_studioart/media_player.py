"""Media player for Revox STUDIOART."""

from __future__ import annotations

import asyncio
import time

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
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SOURCE_COMMANDS, SOURCE_ID_TO_NAME, SOURCE_IDS
from .coordinator import RevoxCoordinator
from .entity import RevoxEntity

# Everything selectable: numeric-id sources (app mechanism) plus the
# documented ASCII sources. Names overlapping in both maps prefer the id.
SOURCE_LIST: list[str] = sorted(set(SOURCE_IDS) | set(SOURCE_COMMANDS))

# No TURN_ON/TURN_OFF: the speaker has no usable power state (STBY is 1 even
# while playing) and no wake command, so a power button would do nothing.
SUPPORT = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
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
    _attr_source_list = SOURCE_LIST

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._unique_base}_media_player"
        self._last_source: str | None = None
        self._volume_before_mute: int | None = None
        # optimistic state shown after a play/pause command until the device
        # confirms it (push or poll) or the deadline passes — streaming
        # sources can take a few seconds to actually transition
        self._pending_state: MediaPlayerState | None = None
        self._pending_until = 0.0

    @property
    def state(self) -> MediaPlayerState:
        st = self.coordinator.data
        if st is None or not st.available:
            return MediaPlayerState.OFF
        # NB: the device's STBY flag is 1 even while actively playing, so it
        # cannot be used for the power state. Play states (verified):
        # 0 = stopped/idle, 1 = playing, 2 = paused.
        actual = {1: MediaPlayerState.PLAYING, 2: MediaPlayerState.PAUSED}.get(
            st.play_state or 0, MediaPlayerState.IDLE
        )
        if self._pending_state is not None:
            if actual == self._pending_state or time.monotonic() >= self._pending_until:
                return actual  # confirmed, or the device never followed
            return self._pending_state
        return actual

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
        if st is not None and st.source in SOURCE_ID_TO_NAME:
            return SOURCE_ID_TO_NAME[st.source]
        return self._last_source

    # -- now-playing metadata (playback JSON + PlayView pushes) -------------
    @property
    def media_content_type(self) -> MediaType | None:
        st = self.coordinator.data
        return MediaType.MUSIC if st and st.media_title else None

    @property
    def media_title(self) -> str | None:
        st = self.coordinator.data
        return st.media_title if st else None

    @property
    def media_artist(self) -> str | None:
        st = self.coordinator.data
        return st.media_artist if st and st.media_title else None

    @property
    def media_album_name(self) -> str | None:
        st = self.coordinator.data
        return st.media_album if st and st.media_title else None

    @property
    def media_image_url(self) -> str | None:
        st = self.coordinator.data
        return st.media_image_url if st and st.media_title else None

    @property
    def media_duration(self) -> int | None:
        st = self.coordinator.data
        if st is None or not st.media_title or st.media_duration_ms is None:
            return None
        return round(st.media_duration_ms / 1000)

    @property
    def media_position(self) -> int | None:
        st = self.coordinator.data
        if (
            st is None
            or not st.media_title
            or st.media_position_ms is None
            or self.state not in (MediaPlayerState.PLAYING, MediaPlayerState.PAUSED)
        ):
            return None
        return round(st.media_position_ms / 1000)

    @property
    def media_position_updated_at(self):
        st = self.coordinator.data
        if st is None or st.media_position_ts is None or self.media_position is None:
            return None
        return dt_util.utc_from_timestamp(st.media_position_ts)

    @property
    def extra_state_attributes(self) -> dict:
        st = self.coordinator.data
        if st is None:
            return {}
        return {
            "battery": st.battery,
            "standby_flag": st.standby,
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
        self._last_source = source
        if source in SOURCE_IDS:
            # numeric id, exactly like the app's Source tab
            await self.coordinator.async_command(
                self.coordinator.client.select_source_id(SOURCE_IDS[source])
            )
            return
        cmd = SOURCE_COMMANDS.get(source)
        if cmd:
            await self.coordinator.async_command(
                self.coordinator.client.select_source(cmd)
            )

    async def _play_pause(self, playing: bool) -> None:
        """Send play/pause and show the target state until confirmed.

        The optimistic state sticks until the device reports it (play-state
        pushes usually confirm within a second or two) or the deadline
        passes, so an early poll cannot flip the UI back and forth.
        """
        self._pending_state = (
            MediaPlayerState.PLAYING if playing else MediaPlayerState.PAUSED
        )
        self._pending_until = time.monotonic() + 6.0
        self.async_write_ha_state()
        client = self.coordinator.client
        await (client.play() if playing else client.pause())
        # confirming poll; further confirmation arrives via pushes
        await asyncio.sleep(1.0)
        await self.coordinator.async_refresh()

    async def async_media_play(self) -> None:
        await self._play_pause(True)

    async def async_media_pause(self) -> None:
        await self._play_pause(False)

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        if media_type in (MediaType.URL, MediaType.MUSIC, "url", "audio/mp3"):
            await self.coordinator.async_command(
                self.coordinator.client.play_url(media_id)
            )
