"""Media player for Revox STUDIOART."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import RevoxState
from .const import SOURCE_COMMANDS, SOURCE_ID_TO_NAME, SOURCE_IDS
from .coordinator import RevoxConfigEntry, RevoxCoordinator
from .entity import RevoxEntity

# commands are serialized by the client's own connection lock
PARALLEL_UPDATES = 0

# How long an optimistic play/pause state is shown before the device must have
# confirmed it; streaming sources can take a few seconds to transition.
PENDING_STATE_SECONDS = 6.0

# Volume to restore on unmute when the pre-mute level is unknown.
DEFAULT_UNMUTE_VOLUME = 20

# Canonical play states (see RevoxState.play_state).
_PLAY_STATES = {1: MediaPlayerState.PLAYING, 2: MediaPlayerState.PAUSED}

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
    | MediaPlayerEntityFeature.BROWSE_MEDIA
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevoxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([RevoxMediaPlayer(entry.runtime_data)])


class RevoxMediaPlayer(RevoxEntity, MediaPlayerEntity):
    """A STUDIOART speaker as a media player."""

    _attr_name = None  # use the device name
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = SUPPORT
    _attr_source_list = SOURCE_LIST

    def __init__(self, coordinator: RevoxCoordinator) -> None:
        super().__init__(coordinator, "media_player")
        self._last_source: str | None = None
        self._volume_before_mute: int | None = None
        # optimistic state shown after a play/pause command until the device
        # confirms it (push or poll) or the deadline passes — streaming
        # sources can take a few seconds to actually transition
        self._pending_state: MediaPlayerState | None = None
        self._pending_until = 0.0

    def _actual_state(self) -> MediaPlayerState:
        """The state as reported by the device, ignoring pending commands."""
        st = self.coordinator.data
        if st is None or not st.available:
            return MediaPlayerState.OFF
        # NB: the device's STBY flag is 1 even while actively playing, so it
        # cannot be used for the power state.
        return _PLAY_STATES.get(st.play_state or 0, MediaPlayerState.IDLE)

    @property
    def state(self) -> MediaPlayerState:
        actual = self._actual_state()
        if self._pending_state is not None and time.monotonic() < self._pending_until:
            return self._pending_state
        return actual

    @callback
    def _handle_coordinator_update(self) -> None:
        # Drop the optimistic state once the device confirms it, so a later
        # change made elsewhere (e.g. pause in the app) shows immediately.
        if self._pending_state == self._actual_state():
            self._pending_state = None
        super()._handle_coordinator_update()

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
    def _track(self) -> RevoxState | None:
        """The state, but only while a track is loaded.

        Artist/album/cover pushes outlive the track they belong to, so every
        metadata field is gated on a current title.
        """
        st = self.coordinator.data
        return st if st is not None and st.media_title else None

    @property
    def media_content_type(self) -> MediaType | None:
        return MediaType.MUSIC if self._track else None

    @property
    def media_title(self) -> str | None:
        return track.media_title if (track := self._track) else None

    @property
    def media_artist(self) -> str | None:
        return track.media_artist if (track := self._track) else None

    @property
    def media_album_name(self) -> str | None:
        return track.media_album if (track := self._track) else None

    @property
    def media_image_url(self) -> str | None:
        return track.media_image_url if (track := self._track) else None

    @property
    def media_duration(self) -> int | None:
        track = self._track
        if track is None or track.media_duration_ms is None:
            return None
        return round(track.media_duration_ms / 1000)

    @property
    def media_position(self) -> int | None:
        track = self._track
        if (
            track is None
            or track.media_position_ms is None
            or self.state not in (MediaPlayerState.PLAYING, MediaPlayerState.PAUSED)
        ):
            return None
        return round(track.media_position_ms / 1000)

    @property
    def media_position_updated_at(self) -> datetime | None:
        st = self.coordinator.data
        if st is None or st.media_position_ts is None or self.media_position is None:
            return None
        return dt_util.utc_from_timestamp(st.media_position_ts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.coordinator.data
        if st is None:
            return {}
        return {
            "battery": st.battery,
            "battery_charging": st.battery_charging,
            "standby_flag": st.standby,
            "wifi_ssid": st.ssid,
            "wifi_rssi": st.rssi,
            "paired_speakers": [p.get("name") for p in st.paired],
            "paired_details": st.paired or None,
            "multiroom_channel": st.channel,
            "kleernet_paired": st.kleernet_paired,
            "kleernet_partner_serial": st.kleernet_partner_serial,
            "ddms_state": st.ddms_state,
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
            restore = self._volume_before_mute or DEFAULT_UNMUTE_VOLUME
            await self.coordinator.async_command(
                self.coordinator.client.set_volume(restore)
            )

    async def async_select_source(self, source: str) -> None:
        client = self.coordinator.client
        if source in SOURCE_IDS:
            # numeric id, exactly like the app's Source tab
            command = client.select_source_id(SOURCE_IDS[source])
        elif source in SOURCE_COMMANDS:
            command = client.select_source(SOURCE_COMMANDS[source])
        else:
            raise ServiceValidationError(
                f"Unknown source {source!r}; choose one of {', '.join(SOURCE_LIST)}"
            )
        await self.coordinator.async_command(command)
        self._last_source = source

    async def _play_pause(self, playing: bool) -> None:
        """Send play/pause and show the target state until confirmed.

        The optimistic state sticks until the device reports it (play-state
        pushes usually confirm within a second or two) or the deadline
        passes, so an early poll cannot flip the UI back and forth.
        """
        client = self.coordinator.client
        self._pending_state = (
            MediaPlayerState.PLAYING if playing else MediaPlayerState.PAUSED
        )
        self._pending_until = time.monotonic() + PENDING_STATE_SECONDS
        self.async_write_ha_state()
        try:
            # one extra poll shortly after, in case no push confirms it
            await self.coordinator.async_command(
                client.play() if playing else client.pause(), settle=1.5
            )
        except HomeAssistantError:
            self._pending_state = None
            self.async_write_ha_state()
            raise

    async def async_media_play(self) -> None:
        await self._play_pause(True)

    async def async_media_pause(self) -> None:
        await self._play_pause(False)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a URL (radio stream, TTS announcement, local media, ...).

        media-source references (e.g. from `tts.speak` or the media browser)
        are resolved to the HTTP URL served by Home Assistant; the speaker
        then streams it via the documented `cmd url`.
        """
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = item.url
        media_id = async_process_play_media_url(self.hass, media_id)
        if not media_id.startswith(("http://", "https://")):
            raise HomeAssistantError(
                f"Only http(s) URLs can be played, got: {media_id}"
            )
        await self.coordinator.async_command(self.coordinator.client.play_url(media_id))

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse Home Assistant media sources (TTS, local media, radio)."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
