"""Unit tests for the wire protocol logic in api.py.

Everything here runs against crafted byte frames and JSON documents — no
speaker and no Home Assistant needed.
"""

from __future__ import annotations

import asyncio
import json
import time


def _client(api):
    return api.RevoxStudioArtClient("192.0.2.1")


# -- frame building ----------------------------------------------------------


def test_build_frame_layout(api):
    # [uint16 length][uint16 group][uint8 cmd][payload]
    assert api._build_frame(2, 0x37) == b"\x00\x03\x00\x02\x37"
    assert api._build_frame(2, 0x2A, b"\x28") == b"\x00\x04\x00\x02\x2a\x28"
    assert api._build_frame(3, 0x03) == b"\x00\x03\x00\x03\x03"


def test_build_event_frame_layout(api):
    # [00 00 VV][OP][00 00 00 00][uint16 len LE][payload]
    frame = api._build_event_frame(0x03)
    assert frame == b"\x00\x00\x02\x03\x00\x00\x00\x00\x00\x00"
    frame = api._build_event_frame(0x6A, b"SETLEFT")
    assert frame[:4] == b"\x00\x00\x02\x6a"
    assert frame[8:10] == b"\x07\x00"  # little-endian length
    assert frame[10:] == b"SETLEFT"
    # the legacy volume query uses protocol version 0x01
    assert api._build_event_frame(0x40, version=0x01)[2] == 0x01


def test_read_frame_roundtrip(api):
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(api._build_frame(2, 0x38, b'{"Name":"X"}'))
        return await api.RevoxStudioArtClient._read_frame(reader)

    group, cmd, payload = asyncio.run(run())
    assert (group, cmd) == (2, 0x38)
    assert json.loads(payload) == {"Name": "X"}


def test_read_event_frame_roundtrip(api):
    async def run():
        reader = asyncio.StreamReader()
        # speaker -> client: [00 00 VV 00][OP][ST][crc16][len16 BE][payload]
        payload = b"19"
        reader.feed_data(
            bytes([0x00, 0x00, 0x02, 0x00, 0x0A, 0x01, 0xBE, 0xEF])
            + len(payload).to_bytes(2, "big")
            + payload
        )
        return await api.RevoxStudioArtClient._read_event_frame(reader)

    op, status, payload = asyncio.run(run())
    assert (op, status, payload) == (0x0A, 0x01, b"19")


# -- battery encoding --------------------------------------------------------


def test_parse_battery(api):
    assert api.parse_battery(None) == (None, None)
    assert api.parse_battery(42) == (42, False)
    assert api.parse_battery(0) == (0, False)
    assert api.parse_battery(254) == (None, True)  # charging, SoC unknown
    assert api.parse_battery(255) == (100, False)  # full / on mains


# -- push event parsing ------------------------------------------------------


def test_parse_event_source_pushes(api):
    client = _client(api)
    assert client._parse_event(0x0A, b"19") == {"source": 19, "_activity": True}
    assert client._parse_event(0x32, b"25") == {"source": 25, "_activity": True}
    assert client._parse_event(0x46, b"SPEAKER_ACTIVE,25") == {
        "source": 25,
        "_activity": True,
    }


def test_parse_event_play_state_enum(api):
    # push enum: 0 = playing/active, 2 = paused (differs from the JSON!)
    client = _client(api)
    assert client._parse_event(0x33, b"0") == {"play_state": 1, "_activity": True}
    assert client._parse_event(0x33, b"2") == {"play_state": 2, "_activity": True}
    assert client._parse_event(0x33, b"1") == {"_activity": True}


def test_parse_event_volume(api):
    client = _client(api)
    assert client._parse_event(0x40, b"55") == {"volume": 55, "_activity": True}


def test_parse_event_channel_status_is_ddms_not_kleernet(api):
    """0x67 reports DDMS (Wi-Fi multi-room) state, never Kleernet pairing.

    A packet capture of a full unpair/re-pair cycle showed this stuck at "FREE"
    while paired[] correctly went [Buero2] -> [] -> [Buero2].
    """
    client = _client(api)
    partial = client._parse_event(0x67, b"FREE,STEREO,RevoxA10028C65AHN")
    assert partial["ddms_state"] == "FREE"
    assert partial["channel"] == "STEREO"
    assert "pair_state" not in partial


def test_parse_event_position_is_instant_play_signal(api):
    client = _client(api)
    # first position tick while not playing -> instant "playing"
    assert client._parse_event(0x31, b"12345") == {"play_state": 1, "_activity": True}
    assert client._media_position[0] == 12345
    # subsequent ticks are cached silently (no state churn per second)
    assert client._parse_event(0x31, b"13345") == {}


def test_parse_playview(api):
    client = _client(api)
    payload = json.dumps(
        {
            "Window CONTENTS": {
                "TrackName": "Track",
                "Artist": "Artist",
                "Album": "Album",
                "CoverArtUrl": "http://cover",
                "TotalTime": 123000,
                "Current Source": 4,
                "PlayState": 0,
            }
        }
    ).encode()
    partial = client._parse_event(0x2A, payload)
    assert partial["media_title"] == "Track"
    assert partial["media_artist"] == "Artist"
    assert partial["media_album"] == "Album"
    assert partial["media_image_url"] == "http://cover"
    assert partial["media_duration_ms"] == 123000
    assert partial["source"] == 4
    assert partial["play_state"] == 1  # push enum: 0 = playing


def test_parse_playview_empty_fields_become_none(api):
    client = _client(api)
    payload = json.dumps(
        {"Window CONTENTS": {"TrackName": "", "Artist": "", "PlayState": 2}}
    ).encode()
    partial = client._parse_event(0x2D, payload)
    assert partial["media_title"] is None
    assert partial["media_artist"] is None
    assert partial["play_state"] == 2


# -- mirror channel ----------------------------------------------------------


def test_mirror_wraps_binary_set_frames(api):
    client = _client(api)
    # payload of an 0x70 push is a complete control frame: [len][group][cmd][data]
    volume_set = api._build_frame(*api.SET_VOLUME, bytes([40]))
    assert client._parse_event(0x70, volume_set) == {
        "volume": 40,
        "_activity": True,
    }


def test_mirror_aux_trigger_is_inverted(api):
    client = _client(api)
    # the wire command is "disable auto aux": 1 = trigger OFF
    frame = api._build_frame(*api.SET_DIS_AUTO_AUX, bytes([1]))
    partial = client._parse_event(0x70, frame)
    assert partial["aux_trigger"] is False
    assert partial["dis_auto_aux"] is True


def test_mirror_get_frames_only_flag_activity(api):
    client = _client(api)
    # a mirrored *get* carries no data byte and must not change state
    get_frame = api._build_frame(2, 0x34)
    assert client._parse_event(0x70, get_frame) == {"_activity": True}


def test_dispatch_event_syncs_toggle_cache(api):
    client = _client(api)
    frame = api._build_frame(*api.SET_LOUDNESS, bytes([1]))
    client._dispatch_event(0x70, 0, frame)
    assert client._toggle_cache["loudness"] is True


# -- poll parsing and push overlay ------------------------------------------

DEV = {
    "Name": "Wohnzimmer",
    "IP": "192.0.2.1",
    "MAC": "aa:bb:cc:dd:ee:ff",
    "SN": "SDHD17496",
    "SSID": "mynet",
    "RSSI": 2,
    "LS9": "V3957",
    "Kleernet": "V1",
    "Controler": "V44",
    "Battery": 254,
    "STBY": 1,
    "volume": 22,
    "Brightness": 50,
    "AutoPowerOn": 1,
    "PowerOnSrc": 0,
}
PLAY = {"source": 19, "state": 1, "volume": 22, "title": "", "albumUrl": ""}
MULTI = {
    "state": 2,
    "LRreverse": 0,
    "paired": [{"type": "A100", "name": "Partner", "battery": 255}],
}
KLEER = {"D83Fre": 0, "DisAutoAux": 1}
TIMER = {"timersty": 15}


def test_state_from_polls(api):
    client = _client(api)
    st = client._state_from_polls(DEV, PLAY, MULTI, KLEER, TIMER)
    assert st.name == "Wohnzimmer"
    assert st.serial == "SDHD17496"
    assert (st.battery, st.battery_charging) == (None, True)  # byte 254
    assert st.standby is True
    assert st.volume == 22
    assert st.source == 19
    assert st.play_state == 1
    assert st.media_title is None  # empty string -> None
    assert st.lr_reverse is False
    assert st.paired[0]["name"] == "Partner"
    assert st.kleernet_band == 0
    assert st.dis_auto_aux is True
    assert st.aux_trigger is False  # inverse of DisAutoAux
    assert st.standby_timer == 15
    assert st.available


def test_overlay_fresh_volume_push_wins(api):
    client = _client(api)
    st = client._state_from_polls(DEV, PLAY, MULTI, KLEER, TIMER)
    client._volume_push = (55, time.monotonic())
    client._overlay_pushed_values(st)
    assert st.volume == 55


def test_overlay_stale_volume_push_ignored(api):
    client = _client(api)
    st = client._state_from_polls(DEV, PLAY, MULTI, KLEER, TIMER)
    client._volume_push = (55, time.monotonic() - 10.0)
    client._overlay_pushed_values(st)
    assert st.volume == 22


def test_overlay_holds_paused_over_lagging_json(api):
    client = _client(api)
    # JSON still reports "playing" right after the paused push
    st = client._state_from_polls(DEV, PLAY, MULTI, KLEER, TIMER)
    client._play_state_push = (2, time.monotonic())
    client._overlay_pushed_values(st)
    assert st.play_state == 2


def test_overlay_json_playing_beats_old_paused_push(api):
    client = _client(api)
    st = client._state_from_polls(DEV, PLAY, MULTI, KLEER, TIMER)
    client._play_state_push = (2, time.monotonic() - 5.0)  # older than 3 s
    client._overlay_pushed_values(st)
    assert st.play_state == 1


def test_overlay_carries_push_only_values(api):
    client = _client(api)
    client._push_cache = {
        "channel": "STEREO",
        "ddms_state": "FREE",
        "media_artist": "Artist",
    }
    st = client._state_from_polls(DEV, PLAY, MULTI, KLEER, TIMER)
    client._overlay_pushed_values(st)
    assert st.channel == "STEREO"
    assert st.ddms_state == "FREE"
    assert st.media_artist == "Artist"


# -- state merging -----------------------------------------------------------


def test_merge_state_applies_fields(api):
    st = api.RevoxState(volume=10, source=19)
    merged = api.merge_state(st, {"volume": 30, "_activity": True})
    assert merged is not st
    assert merged.volume == 30
    assert merged.source == 19  # untouched fields survive


def test_merge_state_noop_returns_same_object(api):
    st = api.RevoxState(volume=10)
    assert api.merge_state(st, {"_activity": True}) is st


def test_state_available(api):
    assert not api.RevoxState().available
    assert api.RevoxState(name="X").available
    assert api.RevoxState(volume=1).available


def test_unpair_speaker_frame(api):
    """group 3 / 0x05 + partner serial = UNPAIR (packet-capture verified).

    Do not rename this to "pair": a capture of the app's unpair/re-pair flow
    showed this command emptying paired[] ~4 s later, while the subsequent
    re-pair produced no port-50007 traffic at all.
    """
    frame = api._build_frame(*api.SET_UNPAIR_SPEAKER, b"SAAD11958")
    # [len u16][group u16][cmd u8][payload]
    assert frame[0:2] == b"\x00\x0c"  # 2 + 1 + 9
    assert frame[2:4] == b"\x00\x03"  # group 3
    assert frame[4] == 0x05
    assert frame[5:] == b"SAAD11958"


def test_kleernet_pair_mode_frame(api):
    """group 3 / 0x01, empty payload, no reply."""
    frame = api._build_frame(*api.CMD_KLEERNET_PAIR_MODE)
    assert frame == b"\x00\x03\x00\x03\x01"


def test_mirrored_pair_set_is_not_misparsed(api):
    """data[0] is 'S' (0x53) — must not be read as a single-byte value."""
    out = api.RevoxStudioArtClient._parse_mirrored_set(3, 0x05, b"SAAD11958")
    assert out == {"_activity": True}
    assert "kleernet_band" not in out and "source" not in out


# -- Kleernet pairing (paired[] is authoritative, not event 0x67) ------------


def _paired(**over):
    entry = {
        "type": "A100",
        "name": "Buero2",
        "ID": "SAAD11958",
        "volume": 12,
        "channel": 1,
        "battery": 255,
    }
    entry.update(over)
    return entry


def test_kleernet_paired_reflects_paired_array(api):
    assert api.RevoxState(paired=[_paired()]).kleernet_paired is True
    assert api.RevoxState(paired=[]).kleernet_paired is False


def test_kleernet_partner_serial_is_the_unpair_payload(api):
    st = api.RevoxState(paired=[_paired()])
    assert st.kleernet_partner_serial == "SAAD11958"
    assert api.RevoxState(paired=[]).kleernet_partner_serial is None
    # an empty ID must not be offered as a serial
    assert api.RevoxState(paired=[_paired(ID="")]).kleernet_partner_serial is None


def test_kleernet_pairing_in_progress_is_channel_zero(api):
    """The capture showed channel pass through 0 while a bind settles."""
    assert api.RevoxState(paired=[_paired(channel=0)]).kleernet_pairing is True
    assert api.RevoxState(paired=[_paired(channel=1)]).kleernet_pairing is False
    assert api.RevoxState(paired=[]).kleernet_pairing is False


def test_ddms_state_does_not_imply_kleernet_pairing(api):
    """The exact situation from the capture: DDMS FREE while a partner is bound."""
    st = api.RevoxState(paired=[_paired()], ddms_state="FREE")
    assert st.ddms_state == "FREE"
    assert st.kleernet_paired is True


def test_kleernet_partner_serial_is_read_from_the_device(api):
    """Nothing about unpairing is configured: the serial comes from paired[].

    This is what makes the Unpair button work on anyone's speaker without
    them knowing or typing a serial number.
    """
    st = api.RevoxState(paired=[_paired(ID="SXYZ99999", name="Kitchen")])
    assert st.kleernet_partner_serial == "SXYZ99999"
    assert st.kleernet_partner["name"] == "Kitchen"


def test_kleernet_partners_filters_unusable_entries(api):
    """Entries without an ID cannot be unpaired, so they must not count."""
    st = api.RevoxState(paired=[_paired(), _paired(ID="", name="ghost")])
    assert [p["ID"] for p in st.kleernet_partners] == ["SAAD11958"]
    assert st.kleernet_paired is True
    # exactly one *usable* partner, so it is still unambiguous
    assert st.kleernet_partner_serial == "SAAD11958"


def test_kleernet_partner_is_none_when_ambiguous(api):
    """Several partners: nothing may act without being told which one."""
    st = api.RevoxState(paired=[_paired(), _paired(ID="SBBB22222")])
    assert st.kleernet_paired is True
    assert st.kleernet_partner is None
    assert st.kleernet_partner_serial is None
    assert len(st.kleernet_partners) == 2


def test_kleernet_pairing_checks_every_partner(api):
    st = api.RevoxState(paired=[_paired(channel=1), _paired(ID="SB", channel=0)])
    assert st.kleernet_pairing is True
