"""Async client for the Revox STUDIOART A100/S100 control protocol.

Reverse-engineered from packet captures of the StudioART app plus the
documented ASCII command set. The speaker exposes two TCP ports:

Port 50007 — control. Carries two protocols simultaneously:

1. Binary, length-prefixed request/response (used for status reads and
   settings)::

       [uint16 length][uint16 group][uint8 cmd][payload...]

   ``length`` counts every byte after itself (i.e. 2 + 1 + len(payload)).
   ``group`` is a small namespace id (2 = device/settings, 3 = multi-room).
   Settings follow a triplet: **get = N, reply = N+1, set = N+2** (the set is
   acknowledged with the same reply cmd N+1 carrying the new value). Payloads
   are a single status byte or a UTF-8 JSON object.

   Confirmed triplets/reads (see README for the capture evidence; the
   loudness/aux-trigger assignment was verified against a live speaker):

     group 2,               set 0x03  Select source by numeric id (19 = Bluetooth,
                                     25 = Analog IN; id 1 = active AirPlay
                                     session, display-only — not to be confused
                                     with the group 3 / 0x03 multi-room *get*)
     group 2, 0x28* -> 0x29, set 0x2A  Volume 0-100 (0x29 is also pushed on
                                     every change; the speaker additionally
                                     echoes a console frame group 0x00FF/0xFF
                                     with {"cmd":"set volume:NN OK"})
     group 2, 0x30 -> 0x31          unknown list read (returns "[]")
     group 2, 0x34 -> 0x35, set 0x36  Loudness (0/1)
     group 2, 0x37 -> 0x38          full device status (JSON)
     group 2, 0x3C -> 0x3D          playback state (JSON: source/state/volume)
     group 2, 0x41 -> 0x42, set 0x43  Aux-In trigger high sensitivity (0/1)
     group 2, 0x47 -> 0x48          unknown flag (value 0 in capture)
     group 2, 0x4D -> 0x4E          power action: value 2 = restart
                                     (ack {"poweroff":1}, then the speaker reboots)
     group 2,        0x57, set 0x58  Power-on source (ack {"PowerOnSrc":n};
                                     0 = last played, 1-5 = presets,
                                     6 = Bluetooth, 7 = Analog IN)
     group 2,        0x5A, set 0x5B  Auto power on (ack is JSON {"AutoPowerOn":n})
     group 2,        0x61, set 0x62  Switch L/R channel (0/1; state = LRreverse)
     group 2, 0x8D -> 0x8E          standby timer (JSON {"timersty":n})
     group 2,        0x9A, set 0x9B  Kleernet wireless band (0 = automatic,
                                     1 = 2.4G, 2 = 5.2G, 3 = 5.8G;
                                     state = "D83Fre" in the Kleernet JSON)
     group 2,        0x9D, set 0x9E  Disable auto aux = Aux-In trigger INVERTED
                                     (1 = trigger off; state = "DisAutoAux" in
                                     the group 3 / 0x57 Kleernet JSON)
     group 3, 0x03 -> 0x04          multi-room state (JSON: LRreverse/paired)
     group 3, 0x56 -> 0x57          Kleernet config (JSON: D83Fre/DisAutoAux)
     group 3, 0x0F                  sent by the app for "Check P100" (no reply seen)

2. ASCII "telnet" control (valid from A100 firmware V41+)::

       cmd volume 50\r\n

   Used here for transport (play/pause), presets, max volume and standby.

Port 7777 — event/push channel, message-framed:

   client -> speaker: [00 00 VV][OP][00 00 00 00][uint16 length LE][payload]
   speaker -> client: [00 00 VV 00][OP][ST][uint16 crc][uint16 length BE][payload]

   ``VV`` is 0x02 for most ops (0x01 for the legacy volume query 0x40). The
   client sends the 4 crc bytes as zeros; the speaker fills a 16-bit checksum
   which we do not need to verify. The observed opcodes are the ``_EV_*``
   constants below; the full table with capture evidence lives in the README.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

_LOGGER = logging.getLogger(__name__)

CONTROL_PORT = 50007
EVENT_PORT = 7777

# Binary read commands (group, request_cmd, expected_reply_cmd)
_CMD_DEVICE_STATUS = (2, 0x37, 0x38)
_CMD_PLAYBACK = (2, 0x3C, 0x3D)
_CMD_MULTIROOM = (3, 0x03, 0x04)
_CMD_LOUDNESS = (2, 0x34, 0x35)
_CMD_AUX_HIGH_SENS = (2, 0x41, 0x42)
# Aux-In trigger state is NOT polled directly: it is the inverse of the
# "DisAutoAux" field in the Kleernet JSON below.
_CMD_KLEERNET = (3, 0x56, 0x57)
_CMD_STANDBY_TIMER = (2, 0x8D, 0x8E)

# Reading the loudness/high-sensitivity triplets makes an open StudioART app
# flicker those toggles (it misrenders the mirrored get frames), so we poll
# them rarely and rely on mirror pushes + our own sets in between.
_TOGGLE_POLL_INTERVAL = 60.0

# Binary set commands (group, set_cmd); ack comes back as set_cmd - 1.
SET_SOURCE = (2, 0x03)  # numeric source id (19 = Bluetooth, 25 = Analog IN)
SET_VOLUME = (2, 0x2A)  # 0-100; change notifications arrive as cmd 0x29
SET_LOUDNESS = (2, 0x36)
SET_AUX_HIGH_SENS = (2, 0x43)
SET_POWER_ON_SOURCE = (2, 0x58)
SET_AUTO_POWER_ON = (2, 0x5B)
SET_LR_SWAP = (2, 0x62)
SET_KLEERNET_BAND = (2, 0x9B)  # 0=auto, 1=2.4G, 2=5.2G, 3=5.8G
SET_DIS_AUTO_AUX = (2, 0x9E)  # 1 = Aux-In trigger OFF (inverted)
# power action command (request/reply, not a settings triplet)
CMD_POWER_ACTION = (2, 0x4D)
POWER_ACTION_RESTART = 2
# "Check P100": fire-and-forget probe for a wired P100 partner speaker
CMD_CHECK_P100 = (3, 0x0F)

# Event channel opcodes
_EV_HANDSHAKE = 0x03
_EV_SOURCE_A = 0x0A  # push: ASCII source id, e.g. "19"
_EV_PLAYVIEW_A = 0x2A  # push: "PlayView" JSON with now-playing metadata
_EV_PLAYVIEW_B = 0x2D  # push: duplicate of 0x2A
_EV_POSITION = 0x31  # push: ASCII playback position in ms, ~1/s while playing
_EV_SOURCE_B = 0x32  # push: ASCII source id (sent alongside 0x0A)
# NB: the push enum differs from the playback JSON: 0 = playing/active
# (sent together with SPEAKER_ACTIVE on play), 2 = paused.
_EV_PLAY_STATE = 0x33
_EV_VOLUME = 0x40  # query; also pushed with the ASCII volume on changes
_EV_SPEAKER_ACTIVE = 0x46  # push: "SPEAKER_ACTIVE,<source id>"
_EV_CHANNEL_STATUS = 0x67
_EV_ASCII_CMD = 0x6A
_EV_MIRROR = 0x70
_EV_QUERY = 0xD0
_EV_BT_EVENT = 0xD1  # push: e.g. "btdisconnect"
_EV_SAMPLE_RATE = 0xE6  # push: ASCII sample rate when a stream starts ("48000")
_EV_STREAM_START = 0xEE  # push: empty marker when a stream starts


class RevoxError(Exception):
    """Raised when communication with the speaker fails."""


class _EventIdle(Exception):
    """No push frame arrived within the idle window (not an error)."""


@dataclass
class RevoxState:
    """Snapshot of everything we can read from the speaker."""

    # device status (cmd 0x38)
    name: str | None = None
    ip: str | None = None
    mac: str | None = None
    serial: str | None = None
    ssid: str | None = None
    rssi: int | None = None
    firmware_ls9: str | None = None
    firmware_kleernet: str | None = None
    firmware_controller: str | None = None
    battery: int | None = None
    standby: bool | None = None
    volume: int | None = None
    brightness: int | None = None
    auto_power_on: bool | None = None
    power_on_source: int | None = None
    # playback (cmd 0x3D / event pushes). Canonical play_state:
    # 0 = idle/stopped, 1 = playing, 2 = paused (paused only exists in
    # pushes — the playback JSON reports 0 for it).
    source: int | None = None
    play_state: int | None = None
    # now-playing metadata (playback JSON + "PlayView" pushes 0x2A/0x2D)
    media_title: str | None = None
    media_artist: str | None = None
    media_album: str | None = None
    media_image_url: str | None = None
    media_duration_ms: int | None = None
    media_position_ms: int | None = None
    media_position_ts: float | None = None  # epoch seconds of the position
    # settings toggles
    aux_trigger: bool | None = None
    aux_high_sensitivity: bool | None = None
    loudness: bool | None = None
    # multi-room (cmd 0x04 / event 0x67)
    lr_reverse: bool | None = None
    multiroom_state: int | None = None
    paired: list[dict[str, Any]] = field(default_factory=list)
    channel: str | None = None  # "STEREO" / "LEFT" / "RIGHT"
    pair_state: str | None = None  # e.g. "FREE"
    # Kleernet config (group 3, 0x57)
    kleernet_band: int | None = None  # "D83Fre": 0=auto, 1=2.4G, 2=5.2G, 3=5.8G
    dis_auto_aux: bool | None = None
    # standby timer (group 2, 0x8E: {"timersty":n}, minutes; 0 = none)
    standby_timer: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.name is not None or self.volume is not None


def _build_frame(group: int, cmd: int, payload: bytes = b"") -> bytes:
    body = struct.pack(">H", group) + bytes([cmd]) + payload
    return struct.pack(">H", len(body)) + body


def _build_event_frame(op: int, payload: bytes = b"", version: int = 0x02) -> bytes:
    # [00 00 VV][OP][00 00 00 00][len LE][payload]
    return (
        bytes([0x00, 0x00, version, op])
        + b"\x00\x00\x00\x00"
        + struct.pack("<H", len(payload))
        + payload
    )


def _decode_json(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_byte": payload[0]}


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


class RevoxStudioArtClient:
    """Talks to a single STUDIOART speaker."""

    def __init__(
        self, host: str, port: int = CONTROL_PORT, event_port: int = EVENT_PORT
    ) -> None:
        self._host = host
        self._port = port
        self._event_port = event_port
        self._lock = asyncio.Lock()
        # event channel state
        self._event_task: asyncio.Task | None = None
        self._event_writer: asyncio.StreamWriter | None = None
        self._event_callback: Callable[[dict[str, Any]], None] | None = None
        # throttled toggle reads (see _TOGGLE_POLL_INTERVAL)
        self._toggle_cache: dict[str, bool | None] = {}
        self._last_toggle_poll = 0.0
        # values that only ever arrive as pushes (0x67 channel status); they
        # must survive polls, which would otherwise reset them to None
        self._push_cache: dict[str, Any] = {}
        # last play-state push (canonical value, monotonic timestamp): the
        # playback JSON lags a second or two behind the pushes, so a poll
        # right after a push would report the *old* state and flap the UI
        self._play_state_push: tuple[int, float] | None = None
        # last position push (ms, epoch seconds)
        self._media_position: tuple[int, float] | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def events_connected(self) -> bool:
        return self._event_writer is not None

    # -- low level: control port -------------------------------------------
    async def _open(
        self, port: int | None = None
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        port = port or self._port
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self._host, port), timeout=4.0
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise RevoxError(f"cannot connect to {self._host}:{port}: {err}") from err

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
        header = await asyncio.wait_for(reader.readexactly(2), timeout=4.0)
        length = struct.unpack(">H", header)[0]
        body = await asyncio.wait_for(reader.readexactly(length), timeout=4.0)
        if len(body) < 3:
            raise RevoxError("short frame")
        group = struct.unpack(">H", body[0:2])[0]
        cmd = body[2]
        return group, cmd, body[3:]

    async def _request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        group: int,
        req_cmd: int,
        reply_cmd: int,
    ) -> bytes:
        """Send a binary get and return the raw reply payload."""
        writer.write(_build_frame(group, req_cmd))
        await writer.drain()
        # skip up to a few unrelated frames until we see the reply we want
        for _ in range(6):
            _g, c, payload = await self._read_frame(reader)
            if c == reply_cmd:
                return payload
        raise RevoxError(f"no reply 0x{reply_cmd:02x} for 0x{req_cmd:02x}")

    async def _request_json(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        group: int,
        req_cmd: int,
        reply_cmd: int,
    ) -> dict[str, Any]:
        return _decode_json(
            await self._request(reader, writer, group, req_cmd, reply_cmd)
        )

    async def _request_byte(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        group: int,
        req_cmd: int,
        reply_cmd: int,
    ) -> int | None:
        payload = await self._request(reader, writer, group, req_cmd, reply_cmd)
        return payload[0] if payload else None

    # -- public API: state --------------------------------------------------
    async def async_get_state(self) -> RevoxState:
        """Read everything we know how to read in one session."""
        async with self._lock:
            reader, writer = await self._open()
            try:
                dev = await self._request_json(reader, writer, *_CMD_DEVICE_STATUS)
                play = await self._request_json(reader, writer, *_CMD_PLAYBACK)
                multi = await self._optional_json(reader, writer, _CMD_MULTIROOM)
                kleer = await self._optional_json(reader, writer, _CMD_KLEERNET)
                timer = await self._optional_json(reader, writer, _CMD_STANDBY_TIMER)
                await self._maybe_poll_toggles(reader, writer)
            finally:
                await self._close(writer)

        st = self._state_from_polls(dev, play, multi, kleer, timer)
        self._overlay_pushed_values(st)
        return st

    async def _maybe_poll_toggles(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Refresh the loudness/high-sensitivity cache, at most once a minute.

        See _TOGGLE_POLL_INTERVAL for why these two reads are throttled.
        """
        now = asyncio.get_running_loop().time()
        if (
            self._toggle_cache
            and None not in self._toggle_cache.values()
            and now - self._last_toggle_poll < _TOGGLE_POLL_INTERVAL
        ):
            return
        loud = await self._optional_byte(reader, writer, _CMD_LOUDNESS)
        aux_hs = await self._optional_byte(reader, writer, _CMD_AUX_HIGH_SENS)
        self._toggle_cache = {
            "loudness": _as_bool(loud),
            "aux_high_sensitivity": _as_bool(aux_hs),
        }
        self._last_toggle_poll = now

    def _state_from_polls(
        self,
        dev: dict[str, Any],
        play: dict[str, Any],
        multi: dict[str, Any],
        kleer: dict[str, Any],
        timer: dict[str, Any],
    ) -> RevoxState:
        """Build a state snapshot from the polled JSON documents."""
        st = RevoxState(
            raw={"device": dev, "playback": play, "multiroom": multi, "kleernet": kleer}
        )
        st.name = dev.get("Name")
        st.ip = dev.get("IP")
        st.mac = dev.get("MAC")
        st.serial = dev.get("SN")
        st.ssid = dev.get("SSID")
        st.rssi = dev.get("RSSI")
        st.firmware_ls9 = dev.get("LS9")
        st.firmware_kleernet = dev.get("Kleernet")
        st.firmware_controller = dev.get("Controler")  # note: firmware spelling
        # 255 means "fully charged / on mains" — the official app shows 100%
        battery = dev.get("Battery")
        st.battery = 100 if battery == 255 else battery
        st.standby = _as_bool(dev.get("STBY"))
        st.volume = dev.get("volume", play.get("volume"))
        st.brightness = dev.get("Brightness")
        st.auto_power_on = _as_bool(dev.get("AutoPowerOn"))
        st.power_on_source = dev.get("PowerOnSrc")
        st.source = play.get("source")
        st.play_state = play.get("state")
        st.media_title = play.get("title") or None
        st.media_image_url = play.get("albumUrl") or None
        st.loudness = self._toggle_cache.get("loudness")
        st.aux_high_sensitivity = self._toggle_cache.get("aux_high_sensitivity")
        st.lr_reverse = _as_bool(multi.get("LRreverse"))
        st.multiroom_state = multi.get("state")
        st.paired = multi.get("paired", []) or []
        st.kleernet_band = kleer.get("D83Fre")
        st.dis_auto_aux = _as_bool(kleer.get("DisAutoAux"))
        st.standby_timer = timer.get("timersty")
        # Aux-In trigger is the inverse of "DisAutoAux" (verified on device)
        if st.dis_auto_aux is not None:
            st.aux_trigger = not st.dis_auto_aux
        return st

    def _overlay_pushed_values(self, st: RevoxState) -> None:
        """Fold push-only values and fresh push overrides into ``st``."""
        # values that only arrive via pushes — carry them over polls
        st.channel = self._push_cache.get("channel")
        st.pair_state = self._push_cache.get("pair_state")
        st.media_artist = self._push_cache.get("media_artist")
        st.media_album = self._push_cache.get("media_album")
        st.media_duration_ms = self._push_cache.get("media_duration_ms")
        if self._media_position is not None:
            st.media_position_ms, st.media_position_ts = self._media_position
        # A fresh play-state push outranks the (lagging) playback JSON.
        if self._play_state_push is not None:
            value, when = self._play_state_push
            age = time.monotonic() - when
            if value == 2:
                # "paused" exists only in pushes: the JSON reports 0 for it
                # (and right after the push briefly still reports 1). Hold
                # paused unless the JSON shows real playback again or the
                # push grows old.
                if st.play_state == 1 and age < 3.0 or st.play_state != 1 and age < 30.0:
                    st.play_state = 2
            elif age < 1.5:
                st.play_state = value

    async def _optional_json(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cmd_triplet: tuple[int, int, int],
    ) -> dict[str, Any]:
        try:
            return await self._request_json(reader, writer, *cmd_triplet)
        except (RevoxError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return {}

    async def _optional_byte(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cmd_triplet: tuple[int, int, int],
    ) -> int | None:
        try:
            return await self._request_byte(reader, writer, *cmd_triplet)
        except (RevoxError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None

    # -- public API: control --------------------------------------------------
    async def _oneshot(
        self,
        data: bytes,
        *,
        read_ascii_reply: bool = False,
        read_ack_frame: bool = False,
    ) -> str | None:
        """Open a control connection, send ``data``, optionally read a reply.

        Both reply styles are best-effort: the speaker does not acknowledge
        every command, so missing replies are not an error.
        """
        async with self._lock:
            reader, writer = await self._open()
            try:
                writer.write(data)
                await writer.drain()
                if read_ascii_reply:
                    try:
                        raw = await asyncio.wait_for(reader.read(256), timeout=1.5)
                        return raw.decode("utf-8", "replace").strip()
                    except (asyncio.TimeoutError, OSError):
                        return None
                if read_ack_frame:
                    with contextlib.suppress(
                        RevoxError, asyncio.TimeoutError, asyncio.IncompleteReadError
                    ):
                        await asyncio.wait_for(self._read_frame(reader), timeout=1.5)
                else:
                    # give the speaker a moment to act before dropping the socket
                    await asyncio.sleep(0.05)
                return None
            finally:
                await self._close(writer)

    async def async_send_cmd(self, command: str, expect_reply: bool = False) -> str | None:
        """Send an ASCII ``cmd ...`` control command.

        ``command`` is the text after ``cmd `` (e.g. ``"volume 50"``).
        """
        return await self._oneshot(
            f"cmd {command}\r\n".encode(), read_ascii_reply=expect_reply
        )

    async def async_set_bin(self, group: int, set_cmd: int, value: int) -> None:
        """Send a binary *set* command; the speaker acks with set_cmd - 1."""
        await self._oneshot(
            _build_frame(group, set_cmd, bytes([value & 0xFF])), read_ack_frame=True
        )

    async def async_send_raw_ascii(self, text: str) -> None:
        """Send a bare ASCII line on the control port (fallback path)."""
        await self._oneshot(f"{text}\r\n".encode())

    # -- convenience controls ---------------------------------------------
    async def set_volume(self, volume: int) -> None:
        # binary volume set as used by the app's Play tab
        await self.async_set_bin(*SET_VOLUME, max(0, min(100, int(volume))))

    async def volume_up(self) -> None:
        await self.async_send_cmd("volup")

    async def volume_down(self) -> None:
        await self.async_send_cmd("voldown")

    async def set_max_volume(self, limit: int) -> None:
        await self.async_send_cmd(f"maxvolume {max(1, min(100, int(limit)))}")

    async def select_source(self, ascii_cmd: str) -> None:
        await self.async_send_cmd(ascii_cmd)

    async def select_source_id(self, source_id: int) -> None:
        """Switch to a numeric source id, as the app's Source tab does."""
        await self.async_set_bin(*SET_SOURCE, source_id)

    async def play(self) -> None:
        await self.async_send_cmd("play")

    async def pause(self) -> None:
        await self.async_send_cmd("pause")

    async def play_url(self, url: str) -> None:
        await self.async_send_cmd(f"url {url}")

    async def standby(self) -> None:
        await self.async_send_cmd("timerstandby")

    async def power_off(self) -> None:
        await self.async_send_cmd("power")

    async def set_bass_boost(self, on: bool) -> None:
        # NB: the documented keyword is misspelled "basssboost" (three s).
        await self.async_send_cmd(f"basssboost {1 if on else 0}")

    async def set_loudness(self, on: bool) -> None:
        await self.async_set_bin(*SET_LOUDNESS, 1 if on else 0)
        self._toggle_cache["loudness"] = on

    async def set_aux_trigger(self, on: bool) -> None:
        # the wire command is "disable auto aux", so the value is inverted
        await self.async_set_bin(*SET_DIS_AUTO_AUX, 0 if on else 1)

    async def set_aux_high_sensitivity(self, on: bool) -> None:
        await self.async_set_bin(*SET_AUX_HIGH_SENS, 1 if on else 0)
        self._toggle_cache["aux_high_sensitivity"] = on

    async def set_auto_power_on(self, on: bool) -> None:
        await self.async_set_bin(*SET_AUTO_POWER_ON, 1 if on else 0)

    async def set_lr_swap(self, on: bool) -> None:
        await self.async_set_bin(*SET_LR_SWAP, 1 if on else 0)

    async def set_power_on_source(self, index: int) -> None:
        await self.async_set_bin(*SET_POWER_ON_SOURCE, index)

    async def set_kleernet_band(self, band: int) -> None:
        await self.async_set_bin(*SET_KLEERNET_BAND, band)

    async def restart(self) -> None:
        """Reboot the speaker (power action 0x4D, value 2)."""
        await self.async_set_bin(*CMD_POWER_ACTION, POWER_ACTION_RESTART)

    async def check_p100(self) -> None:
        """"Check P100" (group 3 / 0x0F): probe whether a wired P100 partner
        speaker is connected to the A100. No reply is sent on the wire."""
        await self._oneshot(_build_frame(*CMD_CHECK_P100), read_ack_frame=True)

    async def set_channel(self, channel_cmd: str) -> None:
        """SETSTEREO / SETLEFT / SETRIGHT — via the event channel like the app.

        Falls back to a bare ASCII line on the control port if the event
        channel is not connected.
        """
        if self._event_writer is not None:
            try:
                await self.async_send_event_ascii(channel_cmd)
                return
            except (OSError, RevoxError):
                _LOGGER.debug("event channel send failed, falling back to control port")
        await self.async_send_raw_ascii(channel_cmd)

    # -- event channel (port 7777) ------------------------------------------
    def start_events(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Start the push listener; ``callback`` receives partial-state dicts."""
        self._event_callback = callback
        if self._event_task is None or self._event_task.done():
            self._event_task = asyncio.get_running_loop().create_task(
                self._event_loop(), name=f"revox_studioart events {self._host}"
            )

    async def stop_events(self) -> None:
        task = self._event_task
        self._event_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not raise
                _LOGGER.debug("event task raised on shutdown", exc_info=True)

    async def async_send_event_ascii(self, text: str) -> None:
        """Send a bare ASCII command via the event channel (op 0x6A)."""
        writer = self._event_writer
        if writer is None:
            raise RevoxError("event channel not connected")
        writer.write(_build_event_frame(_EV_ASCII_CMD, text.encode("utf-8")))
        await writer.drain()

    async def async_event_query(self, text: str) -> str | None:
        """One-shot ASCII query on the event channel (op 0xD0), own connection.

        e.g. ``READ_fwdownload_xml`` -> ``fwdownload_xml:<url>``.
        """
        reader, writer = await self._open(self._event_port)
        try:
            writer.write(_build_event_frame(_EV_HANDSHAKE))
            writer.write(_build_event_frame(_EV_QUERY, text.encode("utf-8")))
            await writer.drain()

            async def _wait_for_reply() -> str:
                while True:
                    op, _status, payload = await self._read_event_frame(reader)
                    if op == _EV_QUERY:
                        return payload.decode("utf-8", "replace")

            try:
                return await asyncio.wait_for(_wait_for_reply(), timeout=4.0)
            except (asyncio.TimeoutError, _EventIdle):
                return None
        finally:
            await self._close(writer)

    @staticmethod
    async def _read_event_frame(
        reader: asyncio.StreamReader,
    ) -> tuple[int, int, bytes]:
        """Read one speaker->client event frame; returns (op, status, payload).

        Raises ``_EventIdle`` if no frame starts within the idle window; a
        timeout *inside* a frame means the stream is broken and raises.
        """
        try:
            header = await asyncio.wait_for(reader.readexactly(10), timeout=30.0)
        except asyncio.TimeoutError as err:
            raise _EventIdle from err
        # [00 00 VV 00][OP][ST][crc16][len16 BE]
        op = header[4]
        status = header[5]
        length = struct.unpack(">H", header[8:10])[0]
        payload = b""
        if length:
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=4.0)
        return op, status, payload

    async def _event_loop(self) -> None:
        backoff = 5.0
        while True:
            try:
                reader, writer = await self._open(self._event_port)
            except RevoxError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            self._event_writer = writer
            backoff = 5.0
            _LOGGER.debug("%s: event channel connected", self._host)
            try:
                # subscribe handshake, mirroring the official app
                writer.write(_build_event_frame(_EV_HANDSHAKE))
                writer.write(_build_event_frame(_EV_VOLUME, version=0x01))
                await writer.drain()
                while True:
                    try:
                        op, status, payload = await self._read_event_frame(reader)
                    except _EventIdle:
                        # idle is normal; poke the speaker so dead links surface
                        writer.write(_build_event_frame(_EV_VOLUME, version=0x01))
                        await writer.drain()
                        continue
                    self._dispatch_event(op, status, payload)
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.IncompleteReadError, RevoxError):
                _LOGGER.debug("%s: event channel lost, reconnecting", self._host)
            finally:
                self._event_writer = None
                await self._close(writer)
            await asyncio.sleep(backoff)

    def _dispatch_event(self, op: int, status: int, payload: bytes) -> None:
        partial = self._parse_event(op, payload)
        if not partial:
            return
        # keep the throttled toggle cache in sync with mirrored sets
        for key in ("loudness", "aux_high_sensitivity"):
            if key in partial:
                self._toggle_cache[key] = partial[key]
        # remember push-only values so the next poll does not lose them
        for key in (
            "channel",
            "pair_state",
            "media_artist",
            "media_album",
            "media_duration_ms",
        ):
            if key in partial:
                self._push_cache[key] = partial[key]
        if "play_state" in partial:
            self._play_state_push = (partial["play_state"], time.monotonic())
        if self._event_callback is not None:
            self._event_callback(partial)

    def _parse_event(self, op: int, payload: bytes) -> dict[str, Any]:
        """Turn one push frame into a partial-state dict."""
        text = payload.decode("utf-8", "replace") if payload else ""
        if op == _EV_CHANNEL_STATUS and payload:
            # "FREE,STEREO,RevoxA10028C65AHN"
            parts = text.split(",")
            partial: dict[str, Any] = {"_activity": True}
            if len(parts) >= 2:
                partial["pair_state"] = parts[0]
                partial["channel"] = parts[1]
            return partial
        if op in (_EV_SOURCE_A, _EV_SOURCE_B) and text.isdigit():
            return {"source": int(text), "_activity": True}
        if op == _EV_PLAY_STATE and text.isdigit():
            # push enum: 0 = playing/active, 2 = paused (differs from JSON!)
            value = int(text)
            if value == 2:
                return {"play_state": 2, "_activity": True}
            if value == 0:
                return {"play_state": 1, "_activity": True}
            return {"_activity": True}
        if op in (_EV_PLAYVIEW_A, _EV_PLAYVIEW_B):
            return self._parse_playview(payload)
        if op == _EV_POSITION and text.isdigit():
            # position ticks ~1/s while playing: cache them (no state write
            # per tick) and use the first one as an instant playing signal
            self._media_position = (int(text), time.time())
            previous = self._play_state_push
            self._play_state_push = (1, time.monotonic())
            if previous is None or previous[0] != 1:
                return {"play_state": 1, "_activity": True}
            return {}
        if op in (_EV_SAMPLE_RATE, _EV_STREAM_START):
            return {"_activity": True}
        if op == _EV_SPEAKER_ACTIVE and "," in text:
            # "SPEAKER_ACTIVE,25"
            source = text.rsplit(",", 1)[-1]
            if source.isdigit():
                return {"source": int(source), "_activity": True}
            return {"_activity": True}
        if op == _EV_VOLUME and text.isdigit():
            return {"volume": int(text), "_activity": True}
        if op == _EV_BT_EVENT and payload:
            return {"_activity": True}
        if op == _EV_MIRROR and len(payload) >= 2:
            # payload is a complete binary control frame: [len][group][cmd][data]
            length = struct.unpack(">H", payload[0:2])[0]
            body = payload[2 : 2 + length]
            if len(body) < 3:
                return {}
            group = struct.unpack(">H", body[0:2])[0]
            cmd = body[2]
            data = body[3:]
            return self._parse_mirrored_set(group, cmd, data)
        return {}

    @staticmethod
    def _parse_playview(payload: bytes) -> dict[str, Any]:
        """Parse a "PlayView" push (now-playing metadata JSON)."""
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {"_activity": True}
        contents = data.get("Window CONTENTS")
        if not isinstance(contents, dict):
            return {"_activity": True}
        partial: dict[str, Any] = {"_activity": True}
        if "TrackName" in contents:
            partial["media_title"] = contents["TrackName"] or None
        if "Artist" in contents:
            partial["media_artist"] = contents["Artist"] or None
        if "Album" in contents:
            partial["media_album"] = contents["Album"] or None
        if "CoverArtUrl" in contents:
            partial["media_image_url"] = contents["CoverArtUrl"] or None
        total = contents.get("TotalTime")
        if isinstance(total, int) and total > 0:
            partial["media_duration_ms"] = total
        source = contents.get("Current Source")
        if isinstance(source, int):
            partial["source"] = source
        # PlayState uses the push enum: 0 = playing, 2 = paused
        play_state = contents.get("PlayState")
        if play_state == 2:
            partial["play_state"] = 2
        elif play_state == 0:
            partial["play_state"] = 1
        return partial

    @staticmethod
    def _parse_mirrored_set(group: int, cmd: int, data: bytes) -> dict[str, Any]:
        """Map a mirrored binary *set* frame to state fields.

        The mirror wraps commands from any client (the app, another HA
        instance), so this is how we learn about outside changes instantly.
        Mirrored *get* frames carry no data and only flag activity.
        """
        if not data:
            return {"_activity": True} if cmd else {}
        value = data[0]
        if (group, cmd) == SET_SOURCE:
            return {"source": value, "_activity": True}
        if (group, cmd) == SET_VOLUME:
            return {"volume": value, "_activity": True}
        if (group, cmd) == SET_DIS_AUTO_AUX:
            return {
                "aux_trigger": not value,
                "dis_auto_aux": bool(value),
                "_activity": True,
            }
        if (group, cmd) == SET_AUX_HIGH_SENS:
            return {"aux_high_sensitivity": bool(value), "_activity": True}
        if (group, cmd) == SET_LOUDNESS:
            return {"loudness": bool(value), "_activity": True}
        if (group, cmd) == SET_AUTO_POWER_ON:
            return {"auto_power_on": bool(value), "_activity": True}
        if (group, cmd) == SET_LR_SWAP:
            return {"lr_reverse": bool(value), "_activity": True}
        if (group, cmd) == SET_POWER_ON_SOURCE:
            return {"power_on_source": value, "_activity": True}
        if (group, cmd) == SET_KLEERNET_BAND:
            return {"kleernet_band": value, "_activity": True}
        return {"_activity": True}


def merge_state(state: RevoxState, partial: dict[str, Any]) -> RevoxState:
    """Return a copy of ``state`` with the partial push update applied."""
    fields = {k: v for k, v in partial.items() if not k.startswith("_")}
    if not fields:
        return state
    return replace(state, **fields)
