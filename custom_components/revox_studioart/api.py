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

   Confirmed triplets/reads (see docs/PROTOCOL.md for the capture evidence; the
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
     group 3, 0x01                  enter Kleernet pairing mode (no reply; the
                                     bind then runs over the Kleernet radio)
     group 3, 0x03 -> 0x04, set 0x05  multi-room state (JSON: LRreverse/paired);
                                     the set UNPAIRS the partner whose serial
                                     is given as bare ASCII, e.g. b"SAAD11958"
     group 3, 0x0F                  sent by the app for "Check P100" (no reply seen)
     group 3, 0x56 -> 0x57          Kleernet config (JSON: D83Fre/DisAutoAux)

2. ASCII "telnet" control (valid from A100 firmware V41+)::

       cmd volume 50\r\n

   Used here for transport (play/pause), presets, max volume and standby.

Port 7777 — event/push channel, message-framed:

   client -> speaker: [00 00 VV][OP][00 00 00 00][uint16 length LE][payload]
   speaker -> client: [00 00 VV 00][OP][ST][uint16 crc][uint16 length BE][payload]

   ``VV`` is 0x02 for most ops (0x01 for the legacy volume query 0x40). The
   client sends the 4 crc bytes as zeros; the speaker fills a 16-bit checksum
   which we do not need to verify. The observed opcodes are the ``_EV_*``
   constants below; the full table with capture evidence lives in docs/PROTOCOL.md.

Two things worth knowing before extending this client, both learned the hard
way and documented in full in docs/PROTOCOL.md:

* **The speaker is two computers.** Port 7777 is Libre's LUCI protocol, served
  by the Linux/Cast board; port 50007 (groups 2 and 3) is served by a separate
  ATMEL host MCU over a UART. That is why the two halves feel like different
  protocols — they are.
* **Every port-50007 reply is fanned out to all open connections.** A frame
  arriving on our socket is not necessarily an answer to what we asked, so
  replies are matched on the expected cmd rather than simply read in order.
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

# How long to wait for a connection or for the reply to one binary get.
_CONNECT_TIMEOUT = 4.0
_REPLY_TIMEOUT = 4.0

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
# UNPAIR a partner speaker: the SET of the multi-room triplet (get 0x03, reply
# 0x04). Payload is the partner's serial number as ASCII, e.g. b"SAAD11958".
#
# Verified from a packet capture of the app's unpair/re-pair flow: sending this
# with the bound partner's serial emptied paired[] ~4 s later. Pairing does NOT
# happen this way — see kleernet_pair_mode() below.
SET_UNPAIR_SPEAKER = (3, 0x05)
# Sent by the app after unpairing, ~11 s before the partner reappeared. Empty
# payload, never answers. Most likely "enter pairing mode"; the actual bind then
# happens over the Kleernet radio with nothing on the network.
CMD_KLEERNET_PAIR_MODE = (3, 0x01)

# Event channel opcodes.
#
# Port 7777 is Libre's "LUCI" protocol; the Op byte is the LUCI message id.
# Names in comments below are the message names recovered from the LS9 firmware
# daemon (/system/bin/luci_service) — see docs/PROTOCOL.md. The message id ==
# Op mapping was confirmed against every op already verified on the wire.
_EV_HANDSHAKE = 0x03
_EV_SOURCE_A = 0x0A  # push: ASCII source id, e.g. "19" (firmware: "IsAllowedRequest")
_EV_PLAYVIEW_A = 0x2A  # "RemoteUI": PlayView JSON with now-playing metadata
_EV_PLAYVIEW_B = 0x2D  # "RemoteUIPlay": duplicate of 0x2A
_EV_POSITION = 0x31  # "Current Time": ASCII playback position ms, ~1/s while playing
_EV_SOURCE_B = 0x32  # "Current Source": ASCII source id (sent alongside 0x0A)
# NB: the push enum differs from the playback JSON: 0 = playing/active
# (sent together with SPEAKER_ACTIVE on play), 2 = paused.
_EV_PLAY_STATE = 0x33  # "Play Status"
_EV_VOLUME = 0x40  # "volume control": query; also pushed with the ASCII volume
_EV_SPEAKER_ACTIVE = 0x46  # "host App control": push "SPEAKER_ACTIVE,<source id>"
_EV_CHANNEL_STATUS = 0x67  # "ddms status": pair-state, e.g. "FREE,STEREO,<ssid>"
_EV_ASCII_CMD = 0x6A  # "speaker type": carries SETSTEREO/SETLEFT/SETRIGHT/SETFREE/...
_EV_MIRROR = 0x70  # "Tunnel Data": mirrors every control-port frame the speaker sees
_EV_QUERY = 0xD0  # "NV Read": READ_<nvitem> -> "<nvitem>:<value>"
_EV_BT_EVENT = 0xD1  # "BT": push, e.g. "btdisconnect"
_EV_SAMPLE_RATE = 0xE6  # "AUDIO_OUTPUT_FS": ASCII sample rate on stream start ("48000")
_EV_STREAM_START = 0xEE  # observed empty stream-start marker (firmware: FORCED_UPDATE?)

# Recovered from firmware, not yet replayed on hardware (see docs/PROTOCOL.md).
# The pairing verbs are ASCII payloads for _EV_ASCII_CMD (0x6A); the rest are
# their own ops. Guarded behind explicit methods / the CLI, never sent
# automatically.
_EV_NET_STANDBY_START = 0x16  # "NET_STANDBY_START"
_EV_NET_STANDBY_END = 0x17  # "NET_STANDBY_END"
_EV_STANDBY_STATUS = 0x18  # "STANDBY_STATUS"
_EV_REBOOT = 0x72  # "RebootRequest"
_EV_WIFI_SCAN = 0x48  # "TriggerWifiScan"
_EV_WIFI_SCAN_RESULTS = 0x49  # "GetWifiScanResults"
_EV_FACTORY_DEFAULT = 0x96  # "FACTORY_DEFAULT" (destructive)
_EV_SPEAKER_FW_UPDATE = 0xEC  # "SPEAKER_FW_UPDATE": the app's "update now" trigger

# Grouping/pairing verbs are split across three ops (decompiled from
# LucicontrolServer::IncomingHouseKeeping) — see docs/PROTOCOL.md:
#   0x64 "ddms":            SETMASTER/SETSLAVE/SETFREE/JOIN*/DROP*
#   0x6A "speaker type":    SETLEFT/SETSTEREO/SETRIGHT  (_EV_ASCII_CMD above)
#   0x6C "StereoPair Mode": MASTERLEFT/MASTERRIGHT/SLAVELEFT/SLAVERIGHT
# The parser length-checks each verb exactly, so send it bare (no NUL/CRLF).
_EV_DDMS = 0x64
_EV_STEREOPAIR_MODE = 0x6C
CHANNEL_UNPAIR = "SETFREE"  # release the speaker from any pair/group (via 0x64)

# Mirrored binary sets (event op 0x70) that map 1:1 onto a state field. The set
# frame's single value byte is passed through the converter. Multi-byte sets
# (the unpair serial) and the inverted aux trigger are handled separately.
_MIRRORED_SETS: dict[tuple[int, int], tuple[str, Callable[[int], Any]]] = {
    SET_SOURCE: ("source", int),
    SET_VOLUME: ("volume", int),
    SET_LOUDNESS: ("loudness", bool),
    SET_AUX_HIGH_SENS: ("aux_high_sensitivity", bool),
    SET_AUTO_POWER_ON: ("auto_power_on", bool),
    SET_LR_SWAP: ("lr_reverse", bool),
    SET_POWER_ON_SOURCE: ("power_on_source", int),
    SET_KLEERNET_BAND: ("kleernet_band", int),
}

# Fields that only ever arrive as pushes. They are cached in the client and
# carried into every polled state, which would otherwise reset them to None
# and make the entities flicker to "unknown" on each scan.
_PUSH_ONLY_FIELDS = (
    "channel",
    "ddms_state",
    "media_artist",
    "media_album",
    "media_duration_ms",
)

# Toggles whose gets are throttled (see _TOGGLE_POLL_INTERVAL); pushes and our
# own sets keep the cached value current in between.
_TOGGLE_FIELDS = ("loudness", "aux_high_sensitivity")


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
    battery: int | None = None  # SoC %, None while charging (SoC not reported)
    battery_charging: bool | None = None
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
    # DDMS (Wi-Fi multi-room) pair state from event 0x67, e.g. "FREE".
    # NOT the Kleernet pairing state: a packet capture of a full unpair/re-pair
    # cycle showed this stuck at "FREE" the whole time while paired[] correctly
    # went [Büro2] -> [] -> [Büro2]. Use kleernet_paired below instead.
    ddms_state: str | None = None
    # Kleernet config (group 3, 0x57)
    kleernet_band: int | None = None  # "D83Fre": 0=auto, 1=2.4G, 2=5.2G, 3=5.8G
    dis_auto_aux: bool | None = None
    # standby timer (group 2, 0x8E: {"timersty":n}, minutes; 0 = none)
    standby_timer: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.name is not None or self.volume is not None

    # -- Kleernet pairing ----------------------------------------------------
    # The authoritative view is the paired[] array from group 3 / 0x03, which a
    # packet capture confirmed tracks the real bind (and briefly reports
    # channel 0 while a pairing completes). Event 0x67 is DDMS and does not.

    @property
    def primary_partner(self) -> dict[str, Any] | None:
        """The first paired[] entry, for display (name, battery, volume).

        Unlike :attr:`kleernet_partner` this does not require a serial or an
        unambiguous single partner — it is what the app shows as "the" client.
        """
        return self.paired[0] if self.paired else None

    @property
    def kleernet_partners(self) -> list[dict[str, Any]]:
        """Bound partners that carry a serial — the payload unpairing needs.

        Entries without an ``ID`` cannot be unpaired, so they are filtered out
        here once rather than at each call site.
        """
        return [p for p in self.paired if p.get("ID")]

    @property
    def kleernet_paired(self) -> bool:
        """True when at least one Kleernet partner speaker is bound."""
        return bool(self.kleernet_partners)

    @property
    def kleernet_partner(self) -> dict[str, Any] | None:
        """The single bound partner, or None if there are zero or several.

        Returning None for "several" is deliberate: unpairing needs one
        specific serial, so anything that acts without being told which
        partner to use must only do so when the choice is unambiguous.
        """
        partners = self.kleernet_partners
        return partners[0] if len(partners) == 1 else None

    @property
    def kleernet_partner_serial(self) -> str | None:
        """Serial of the single bound partner, if unambiguous."""
        partner = self.kleernet_partner
        return partner["ID"] if partner else None

    @property
    def kleernet_pairing(self) -> bool:
        """True while a bind is still settling (a partner reports channel 0)."""
        return any(p.get("channel") == 0 for p in self.kleernet_partners)


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


def _push_play_state(value: Any) -> int | None:
    """Map the push play-state enum onto the canonical one.

    Pushes (op 0x33 and PlayView "PlayState") use 0 = playing/active and
    2 = paused, unlike the playback JSON (0 = stopped, 1 = playing). Other
    values carry no usable state.
    """
    return {0: 1, 2: 2}.get(value)


def parse_battery(raw: int | None) -> tuple[int | None, bool | None]:
    """Decode the battery byte into (SoC percent, charging).

    0-100 = state of charge, 254 = charging (the SoC is not reported while
    charging — the app shows just "Charging"), 255 = fully charged / on mains.
    """
    if raw is None:
        return None, None
    if raw == 254:
        return None, True
    if raw == 255:
        return 100, False
    return raw, False


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
        # last volume push (value, monotonic timestamp): during a volume ramp
        # the device JSON trails the pushes by a few hundred ms, so polls in
        # between would make the slider jump backwards
        self._volume_push: tuple[int, float] | None = None

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
                asyncio.open_connection(self._host, port), timeout=_CONNECT_TIMEOUT
            )
        except (TimeoutError, OSError) as err:
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
        # Replies to other clients (the app polling in parallel) are fanned
        # out to us too, so skip unrelated frames until the deadline rather
        # than giving up after a fixed number of them.
        try:
            async with asyncio.timeout(_REPLY_TIMEOUT):
                while True:
                    g, c, payload = await self._read_frame(reader)
                    if (g, c) == (group, reply_cmd):
                        return payload
        except TimeoutError as err:
            raise RevoxError(f"no reply 0x{reply_cmd:02x} for 0x{req_cmd:02x}") from err

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
            raw={
                "device": dev,
                "playback": play,
                "multiroom": multi,
                "kleernet": kleer,
                "standby_timer": timer,
            }
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
        st.battery, st.battery_charging = parse_battery(dev.get("Battery"))
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
        for key in _PUSH_ONLY_FIELDS:
            setattr(st, key, self._push_cache.get(key))
        if self._media_position is not None:
            st.media_position_ms, st.media_position_ts = self._media_position
        # A fresh volume push outranks the (trailing) JSON; each push renews
        # the window, so during a ramp the pushes rule continuously.
        if self._volume_push is not None:
            value, when = self._volume_push
            if time.monotonic() - when < 2.0:
                st.volume = value
        # A fresh play-state push outranks the (lagging) playback JSON.
        if self._play_state_push is not None:
            value, when = self._play_state_push
            age = time.monotonic() - when
            if value == 2:
                # "paused" exists only in pushes: the JSON reports 0 for it
                # (and right after the push briefly still reports 1). Hold
                # paused unless the JSON shows real playback again or the
                # push grows old.
                if (st.play_state == 1 and age < 3.0) or (
                    st.play_state != 1 and age < 30.0
                ):
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
        except (TimeoutError, RevoxError, asyncio.IncompleteReadError):
            return {}

    async def _optional_byte(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cmd_triplet: tuple[int, int, int],
    ) -> int | None:
        try:
            return await self._request_byte(reader, writer, *cmd_triplet)
        except (TimeoutError, RevoxError, asyncio.IncompleteReadError):
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
                    except (TimeoutError, OSError):
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

    async def async_send_cmd(
        self, command: str, expect_reply: bool = False
    ) -> str | None:
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
        value = max(0, min(100, int(volume)))
        await self.async_set_bin(*SET_VOLUME, value)
        self._volume_push = (value, time.monotonic())

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
        """ "Check P100" (group 3 / 0x0F): probe whether a wired P100 partner
        speaker is connected to the A100. No reply is sent on the wire."""
        await self._oneshot(_build_frame(*CMD_CHECK_P100), read_ack_frame=True)

    async def kleernet_unpair(self, serial: str) -> None:
        """Unpair a Kleernet partner speaker by serial number (group 3 / 0x05).

        ``serial`` is the partner's SN as it appears in the multi-room
        ``paired[]`` array (``ID``), e.g. ``"SAAD11958"``. Packet-capture
        verified: ``paired[]`` empties roughly four seconds later, so poll
        rather than assuming the change is immediate.

        This is *not* how pairing happens — see :meth:`kleernet_pair_mode`.
        """
        serial = serial.strip().upper()
        if not serial or not serial.isascii():
            raise RevoxError(f"invalid serial number: {serial!r}")
        payload = serial.encode("ascii")
        await self._oneshot(
            _build_frame(*SET_UNPAIR_SPEAKER, payload), read_ack_frame=True
        )

    async def kleernet_pair_mode(self) -> None:
        """Ask the speaker to enter Kleernet pairing mode (group 3 / 0x01).

        Fire-and-forget: no reply is sent. In the reference capture the app sent
        this after unpairing and the partner reappeared ~11 s later, with no
        further network traffic — the bind itself happens over the Kleernet
        radio. Interpretation is capture-derived and not independently
        confirmed; the partner may also need putting into pairing mode.
        """
        await self._oneshot(_build_frame(*CMD_KLEERNET_PAIR_MODE), read_ack_frame=True)

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

    async def unpair(self) -> None:
        """Release the speaker from any stereo pair / multi-room group.

        ``SETFREE`` is handled by the *ddms* op (0x64), NOT by the ``speaker
        type`` op (0x6A) that carries SETSTEREO/SETLEFT/SETRIGHT — 0x6A only
        compares the three channel verbs and silently ignores anything else.
        The parser also length-checks the payload exactly, so the verb is sent
        bare (no NUL, no CRLF). Firmware-derived; see docs/PROTOCOL.md.
        """
        await self.async_send_event_op(_EV_DDMS, CHANNEL_UNPAIR.encode("ascii"))

    async def async_send_event_op(
        self, op: int, payload: bytes = b"", *, expect_reply: bool = False
    ) -> str | None:
        """Send a raw LUCI op on the event channel (own one-shot connection).

        Escape hatch for the firmware-recovered ops that don't yet have a
        dedicated method (reboot 0x72, net-standby 0x16/0x17, fw-update 0xEC,
        wifi-scan 0x48/0x49, ...). Intended for verification via the CLI, not
        for automatic use.
        """
        reader, writer = await self._open(self._event_port)
        try:
            writer.write(_build_event_frame(_EV_HANDSHAKE))
            writer.write(_build_event_frame(op, payload))
            await writer.drain()
            if not expect_reply:
                return None

            try:
                async with asyncio.timeout(_REPLY_TIMEOUT):
                    while True:
                        rop, _st, rpl = await self._read_event_frame(reader)
                        if rop == op:
                            return rpl.decode("utf-8", "replace")
            except (TimeoutError, _EventIdle):
                return None
        finally:
            await self._close(writer)

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
            except Exception:
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
        return await self.async_send_event_op(
            _EV_QUERY, text.encode("utf-8"), expect_reply=True
        )

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
        except TimeoutError as err:
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
        for key in _TOGGLE_FIELDS:
            if key in partial:
                self._toggle_cache[key] = partial[key]
        # remember push-only values so the next poll does not lose them
        for key in _PUSH_ONLY_FIELDS:
            if key in partial:
                self._push_cache[key] = partial[key]
        if "play_state" in partial:
            self._play_state_push = (partial["play_state"], time.monotonic())
        if "volume" in partial:
            self._volume_push = (partial["volume"], time.monotonic())
        if self._event_callback is not None:
            self._event_callback(partial)

    def _parse_event(self, op: int, payload: bytes) -> dict[str, Any]:
        """Turn one push frame into a partial-state dict."""
        text = payload.decode("utf-8", "replace") if payload else ""
        if op == _EV_CHANNEL_STATUS and payload:
            # "FREE,STEREO,RevoxA10028C65AHN". NB: this is the DDMS (Wi-Fi
            # multi-room) state, not Kleernet. The speaker sends it after
            # *every* group 3 / 0x03 get (ours included), so it only counts
            # as activity when something actually changed — otherwise each
            # poll would trigger the next one.
            parts = text.split(",")
            if len(parts) < 2:
                return {}
            partial: dict[str, Any] = {"ddms_state": parts[0], "channel": parts[1]}
            if any(self._push_cache.get(k) != v for k, v in partial.items()):
                partial["_activity"] = True
            return partial
        if op in (_EV_SOURCE_A, _EV_SOURCE_B) and text.isdigit():
            # "0" is a transient "no source" sent at the start of a switch or
            # an AirPlay session, before the real id; the playback JSON never
            # reports it, so ignore it rather than blank the source.
            if text == "0":
                return {}
            return {"source": int(text), "_activity": True}
        if op == _EV_PLAY_STATE and text.isdigit():
            partial = {"_activity": True}
            if (value := _push_play_state(int(text))) is not None:
                partial["play_state"] = value
            return partial
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
        if (play_state := _push_play_state(contents.get("PlayState"))) is not None:
            partial["play_state"] = play_state
        return partial

    @staticmethod
    def _parse_mirrored_set(group: int, cmd: int, data: bytes) -> dict[str, Any]:
        """Map a mirrored binary *set* frame to state fields.

        The mirror wraps commands from any client (the app, another HA
        instance), so this is how we learn about outside changes instantly.

        Frames without data are gets (or fire-and-forget actions) and are
        ignored: the mirror echoes every client's polls — including our own —
        so treating them as activity would make each poll trigger the next.
        """
        if not data:
            return {}
        key = (group, cmd)
        if key == SET_DIS_AUTO_AUX:
            # the wire command is "disable auto aux": 1 = trigger OFF
            return {
                "aux_trigger": not data[0],
                "dis_auto_aux": bool(data[0]),
                "_activity": True,
            }
        if key in _MIRRORED_SETS and len(data) == 1:
            field_name, convert = _MIRRORED_SETS[key]
            return {field_name: convert(data[0]), "_activity": True}
        # Anything else — including the unpair set, whose payload is a
        # multi-byte ASCII serial: the speaker drops the partner a few seconds
        # later, so just flag activity and let the coordinator re-poll.
        return {"_activity": True}


def merge_state(state: RevoxState, partial: dict[str, Any]) -> RevoxState:
    """Return a copy of ``state`` with the partial push update applied.

    Returns ``state`` itself when the push changes nothing, so callers can
    skip notifying listeners.
    """
    fields = {
        k: v
        for k, v in partial.items()
        if not k.startswith("_") and getattr(state, k) != v
    }
    if not fields:
        return state
    return replace(state, **fields)
