#!/usr/bin/env python3
"""Standalone CLI for the Revox STUDIOART A100/S100 control protocol.

No Home Assistant required. Use it to verify control and to identify commands
that don't yet have a confirmed mapping.

Examples
--------
    python3 revox_cli.py 192.168.42.163 status
    python3 revox_cli.py 192.168.42.163 volume 40
    python3 revox_cli.py 192.168.42.163 source aux
    python3 revox_cli.py 192.168.42.163 srcid 19          # numeric source id
    python3 revox_cli.py 192.168.42.163 binvolume 30      # binary volume 0x2A
    python3 revox_cli.py 192.168.42.163 play | pause | standby | power
    python3 revox_cli.py 192.168.42.163 loudness 1        # binary set 0x36
    python3 revox_cli.py 192.168.42.163 aux-trigger 0     # set 0x9E inverted
    python3 revox_cli.py 192.168.42.163 aux-sens 1        # binary set 0x43
    python3 revox_cli.py 192.168.42.163 lrswap 0          # binary set 0x62
    python3 revox_cli.py 192.168.42.163 autopoweron 1     # binary set 0x5B
    python3 revox_cli.py 192.168.42.163 poweronsrc 0      # binary set 0x58
    python3 revox_cli.py 192.168.42.163 kleernet-band 0   # 0=auto 1=2.4G 2=5.2G 3=5.8G
    python3 revox_cli.py 192.168.42.163 restart           # reboot the speaker
    python3 revox_cli.py 192.168.42.163 bassboost 1
    python3 revox_cli.py 192.168.42.163 channel left      # SETLEFT via port 7777
    python3 revox_cli.py 192.168.42.163 cmd "volume 55"   # raw `cmd ...`
    python3 revox_cli.py 192.168.42.163 raw SETSTEREO     # bare ASCII on 50007
    python3 revox_cli.py 192.168.42.163 get 2 0x34        # binary get
    python3 revox_cli.py 192.168.42.163 bin 2 0x5b 1      # binary set
    python3 revox_cli.py 192.168.42.163 readq READ_fwdownload_xml   # 0xD0 query
    python3 revox_cli.py 192.168.42.163 watch             # live push events (7777)
    python3 revox_cli.py 192.168.42.163 kleernet          # full Kleernet/pairing report
    python3 revox_cli.py 192.168.42.163 kleernet-unpair SAAD11958  # group 3/0x05
    python3 revox_cli.py 192.168.42.163 kleernet-pairmode         # group 3/0x01
    python3 revox_cli.py 192.168.42.163 scan 3 0x00 0x40 --i-understand

NV items (LibreEnv property store) via op 0xD0 — READ_/WRITE_ by name. The name
table was recovered from /system/bin/LibreEnv and verified against a real A100;
see docs/PROTOCOL.md.
    python3 revox_cli.py 192.168.42.163 nv FwVersion       # read one item
    python3 revox_cli.py 192.168.42.163 nvdump             # curated monitoring set
    python3 revox_cli.py 192.168.42.163 nvdump live        # items seen populated
    python3 revox_cli.py 192.168.42.163 nvdump all --set   # all 255, hide empties
    python3 revox_cli.py 192.168.42.163 nvdump --show-secrets   # unmask values
    python3 revox_cli.py 192.168.42.163 nvwrite LED_INTENSITY 5
    python3 revox_cli.py - nvlist ddms                     # offline name search
Secrets (PSK, tokens, URLs, UUIDs) are masked by default so output is safe to
paste into an issue. Network/boot-critical items need --force to write.

LUCI ops recovered from the LS9 firmware (firmware-derived — verify on your
speaker; see docs/PROTOCOL.md). These act on the real device:
    python3 revox_cli.py 192.168.42.163 unpair            # SETFREE via 0x64 (ddms)
    python3 revox_cli.py 192.168.42.163 ddms dropme       # raw ddms verb (0x64)
    python3 revox_cli.py 192.168.42.163 pairmode slaveleft  # 0x6C StereoPair Mode
    python3 revox_cli.py 192.168.42.163 net-standby on    # 0x16 / off = 0x17
    python3 revox_cli.py 192.168.42.163 standby-status    # 0x18
    python3 revox_cli.py 192.168.42.163 wifi-scan         # 0x48 (results push on 0x49)
    python3 revox_cli.py 192.168.42.163 reboot-luci       # 0x72 RebootRequest
    python3 revox_cli.py 192.168.42.163 fw-update         # 0xEC SPEAKER_FW_UPDATE
    python3 revox_cli.py 192.168.42.163 event-op 0x97     # generic: <op-hex> [payload]
"""

import json
import socket
import struct
import sys
import time

PORT = 50007
EVENT_PORT = 7777

# (group, get, reply) for the `status` verb
READS = {
    "device": (2, 0x37, 0x38),
    "playback": (2, 0x3C, 0x3D),
    "multiroom": (3, 0x03, 0x04),
    "loudness": (2, 0x34, 0x35),
    "aux_high_sens": (2, 0x41, 0x42),
    "kleernet": (3, 0x56, 0x57),
    "standby_timer": (2, 0x8D, 0x8E),
}

TOGGLES = {  # verb -> (group, set_cmd)
    "loudness": (2, 0x36),
    "aux-sens": (2, 0x43),
    "poweronsrc": (2, 0x58),  # 0=last played, 1-5=presets, 6=BT, 7=analog in
    "autopoweron": (2, 0x5B),
    "lrswap": (2, 0x62),
    "kleernet-band": (2, 0x9B),  # 0=auto, 1=2.4G, 2=5.2G, 3=5.8G
    # "disable auto aux": 1 turns the Aux-In trigger OFF. Prefer the
    # `aux-trigger` verb which handles the inversion for you.
    "disautoaux": (2, 0x9E),
}


def build_frame(group: int, cmd: int, payload: bytes = b"") -> bytes:
    body = struct.pack(">H", group) + bytes([cmd]) + payload
    return struct.pack(">H", len(body)) + body


def read_frame(sock: socket.socket):
    hdr = _recvn(sock, 2)
    if len(hdr) < 2:
        return None
    length = struct.unpack(">H", hdr)[0]
    body = _recvn(sock, length)
    if len(body) < 3:
        return None
    group = struct.unpack(">H", body[0:2])[0]
    cmd = body[2]
    return group, cmd, body[3:]


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _decode(payload: bytes):
    """Best-effort payload decode: JSON, single byte, printable ASCII, else hex.

    The ASCII case matters: group 3 / 0x05 carries a bare serial number
    ("SAAD11958"), which used to render as an opaque hex blob and made the
    pairing command much harder to recognise on the mirror channel.
    """
    if not payload:
        return None
    try:
        return json.loads(payload.decode())
    except Exception:
        pass
    if len(payload) == 1:
        return payload[0]
    if all(0x20 <= b < 0x7F for b in payload):
        return payload.decode("ascii")
    return payload.hex()


def request(host: str, group: int, req_cmd: int, reply_cmd: int):
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(build_frame(group, req_cmd))
        for _ in range(8):
            frame = read_frame(s)
            if not frame:
                break
            _g, c, payload = frame
            if c == reply_cmd:
                return _decode(payload)
    return None


# Commands that are known SETs or fire-and-forget ACTIONS. `scan` sends
# empty-payload frames, which for a set/action is at best ignored and at worst
# performs something (0x4D value 0 = unknown power action). Never probe these.
SCAN_BLOCKLIST = {
    2: {
        0x03,  # select source
        0x2A,  # set volume
        0x36,  # set loudness
        0x43,  # set aux high-sens
        0x4D,  # POWER ACTION (2 = restart) — never poke
        0x58,  # set power-on source
        0x5B,  # set auto-power-on
        0x62,  # set L/R swap
        0x8F,  # presumed standby-timer set
        0x9B,  # set Kleernet band
        0x9E,  # set "disable auto aux"
    },
    3: {
        0x0F,  # Check P100 — fire-and-forget action
    },
}


def probe(host: str, group: int, cmd: int, timeout: float = 1.0):
    """Send one empty-payload frame and return every reply frame it produces."""
    out = []
    try:
        with socket.create_connection((host, PORT), timeout=3) as s:
            s.sendall(build_frame(group, cmd))
            s.settimeout(timeout)
            try:
                while True:
                    frame = read_frame(s)
                    if not frame:
                        break
                    out.append(frame)
            except (TimeoutError, OSError):
                pass
    except OSError as err:
        print(f"  connect failed: {err}")
    return out


def scan(
    host: str,
    group: int,
    start: int,
    end: int,
    delay: float = 0.15,
    timeout: float = 1.0,
) -> None:
    """Sweep a command range and report what answers, learning the triplets.

    Discovery aid for the group-2/group-3 protocol, which lives on the ATMEL
    host MCU and so cannot be recovered from the Linux firmware.

    Two hard-won safety properties:

    1. **Sets are inferred as we go.** Settings follow get=N / reply=N+1 /
       set=N+2. So the moment a probe of N answers with N+1, we know N+2 is a
       set and skip it. An empty-payload set writes 0 — an early version of this
       function silently zeroed two unknown group-3 settings that way.
       A reply of N-1 means N was itself a set, so we stop immediately.
    2. **Other clients' traffic is filtered out.** The speaker fans replies out
       to *every* open port-50007 connection, so a scan sees Home Assistant's
       and the app's poll replies too. Only frames with cmd N or N+1 are
       attributed to our probe; the rest are counted as background.

    NB: a get also shows up on the mirror channel, so an open StudioART app may
    flicker its toggles while this runs. That is cosmetic.
    """
    blocked = set(SCAN_BLOCKLIST.get(group, set()))
    learned: dict[int, int] = {}  # set-cmd -> the get it belongs to
    found = background = 0
    print(
        f"# scanning group {group}, cmds {start:#04x}-{end:#04x} "
        f"({len(blocked)} known sets/actions skipped)"
    )
    for cmd in range(start, end + 1):
        if cmd in blocked:
            print(f"  {cmd:#04x}  (skipped: known set/action)")
            continue
        if cmd in learned:
            print(
                f"  {cmd:#04x}  (skipped: inferred SET of the "
                f"{learned[cmd]:#04x} triplet)"
            )
            continue

        frames = probe(host, group, cmd, timeout=timeout)
        for g, c, payload in frames:
            val = _decode(payload)
            if val is None and not payload:
                continue
            if g != group or c not in (cmd, cmd + 1):
                background += 1
                continue
            found += 1
            rel = "reply" if c == cmd + 1 else "echo"
            print(f"  {cmd:#04x} -> group={g} {rel:<6} {val!r}")
            if c == cmd + 1:
                # cmd is a get; its set is cmd+2 and must not be probed
                learned[cmd + 2] = cmd

        # A reply numbered cmd-1 means we just probed a SET. Bail out rather
        # than keep writing zeros into unknown settings.
        for g, c, _p in frames:
            if g == group and c == cmd - 1:
                print(
                    f"  !! {cmd:#04x} answered as {c:#04x} — that makes "
                    f"{cmd:#04x} a SET, which an empty payload may have "
                    f"written to 0.\n"
                    f"  !! Stopping. Read {c - 1:#04x} and restore with: "
                    f"bin {group} {cmd:#04x} <original>"
                )
                return
        time.sleep(delay)
    print(
        f"# {found} responses to our probes, {background} background frames "
        f"from other clients, {len(learned)} sets inferred and skipped"
    )


# Group 3 has been swept end-to-end (0x00-0xFF). These four triplets answer but
# their meaning is unknown; they are read-only here (get cmd -> reply cmd).
# Diffing them between states — partner paired vs unpaired, band changed, L/R
# swapped — is the way to pin down what they are.
GROUP3_UNKNOWN = {0x06: 0x07, 0x09: 0x0A, 0x0C: 0x0D, 0x10: 0x11}


def kleernet_report(host: str) -> None:
    """Everything currently known about the Kleernet / multi-room state."""
    print("== group 3 / 0x03  multi-room state")
    multi = request(host, 3, 0x03, 0x04)
    print(f"   {multi!r}")
    if isinstance(multi, dict):
        for i, p in enumerate(multi.get("paired") or []):
            print(f"   paired[{i}]: {p!r}")
        if not multi.get("paired"):
            print("   paired[]: (none — no partner speaker bound)")

    print("== group 3 / 0x56  Kleernet config")
    kleer = request(host, 3, 0x56, 0x57)
    print(f"   {kleer!r}")
    if isinstance(kleer, dict):
        band = {0: "automatic", 1: "2.4 GHz", 2: "5.2 GHz", 3: "5.8 GHz"}
        print(f"   band (D83Fre) = {band.get(kleer.get('D83Fre'), '?')}")
        print(f"   Aux-In trigger = {'off' if kleer.get('DisAutoAux') else 'on'}")

    print("== group 3  unidentified triplets (read-only; diff between states)")
    for get_cmd, reply_cmd in sorted(GROUP3_UNKNOWN.items()):
        val = request(host, 3, get_cmd, reply_cmd)
        shown = val.get("value") if isinstance(val, dict) else val
        ident = val.get("ID") if isinstance(val, dict) else None
        extra = f"  ID={ident!r}" if ident else ""
        print(f"   get {get_cmd:#04x} -> value={shown!r}{extra}")

    print("== group 2 / 0x37  device status (Kleernet radio firmware)")
    dev = request(host, 2, 0x37, 0x38)
    if isinstance(dev, dict):
        for k in ("Kleernet", "LS9", "Controler", "mcuType", "SN", "Name"):
            print(f"   {k:<10} {dev.get(k)!r}")

    print("== event 0x67  DDMS/pair status push (2 s listen)")
    try:
        with event_connect(host) as s:
            s.settimeout(2.0)
            try:
                while True:
                    frame = read_event_frame(s)
                    if not frame:
                        break
                    op, status, payload = frame
                    if op in (0x67, 0x46):
                        print(f"   {_fmt_event(op, status, payload)}")
            except TimeoutError:
                pass
    except OSError as err:
        print(f"   event channel unavailable: {err}")


def get_status(host: str) -> dict:
    status = {name: request(host, *spec) for name, spec in READS.items()}
    # Aux-In trigger is the inverse of "DisAutoAux" (verified on device)
    kleer = status.get("kleernet") or {}
    if isinstance(kleer, dict) and "DisAutoAux" in kleer:
        status["aux_trigger"] = 0 if kleer["DisAutoAux"] else 1
    return status


def send_cmd(host: str, text: str) -> None:
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(f"cmd {text}\r\n".encode())
        s.settimeout(1.0)
        try:
            print("reply:", s.recv(256).decode("utf-8", "replace").strip())
        except Exception:
            print("sent:", f"cmd {text}")


def send_raw(host: str, text: str) -> None:
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(f"{text}\r\n".encode())
        s.settimeout(1.0)
        try:
            print("reply:", s.recv(256).decode("utf-8", "replace").strip())
        except Exception:
            print("sent:", text)


def send_frame(
    host: str,
    group: int,
    cmd: int,
    payload: bytes = b"",
    *,
    expect: tuple[int, ...] | None = None,
    timeout: float = 1.5,
) -> None:
    """Send one binary frame and print the ack.

    ``expect`` filters which reply cmds count as ours: the speaker fans every
    reply out to all open connections, so without it another client's poll
    reply can be mistaken for an ack.
    """
    with socket.create_connection((host, PORT), timeout=4) as sock:
        sock.sendall(build_frame(group, cmd, payload))
        sock.settimeout(timeout)
        try:
            while True:
                frame = read_frame(sock)
                if not frame:
                    break
                g, c, data = frame
                if expect is not None and (g != group or c not in expect):
                    continue  # another client's traffic
                print(f"ack: group={g} cmd=0x{c:02x} {_decode(data)!r}")
                return
        except (TimeoutError, OSError):
            pass
    print("no ack")


def send_bin(host: str, group: int, cmd: int, value: int) -> None:
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(build_frame(group, cmd, bytes([value & 0xFF])))
        s.settimeout(1.5)
        try:
            frame = read_frame(s)
            if frame:
                g, c, payload = frame
                print(f"ack: group={g} cmd=0x{c:02x} value={_decode(payload)!r}")
            else:
                print("no ack")
        except Exception:
            print("sent bin:", group, hex(cmd), value)


# -- event channel (port 7777) ------------------------------------------------
def build_event_frame(op: int, payload: bytes = b"", version: int = 0x02) -> bytes:
    # client -> speaker: [00 00 VV][OP][00 00 00 00][len LE][payload]
    return (
        bytes([0x00, 0x00, version, op])
        + b"\x00\x00\x00\x00"
        + struct.pack("<H", len(payload))
        + payload
    )


def read_event_frame(sock: socket.socket):
    # speaker -> client: [00 00 VV 00][OP][ST][crc16][len16 BE][payload]
    hdr = _recvn(sock, 10)
    if len(hdr) < 10:
        return None
    op, status = hdr[4], hdr[5]
    length = struct.unpack(">H", hdr[8:10])[0]
    payload = _recvn(sock, length) if length else b""
    return op, status, payload


def event_connect(host: str) -> socket.socket:
    s = socket.create_connection((host, EVENT_PORT), timeout=4)
    s.sendall(build_event_frame(0x03))  # subscribe handshake
    return s


def event_ascii(host: str, text: str) -> None:
    """Send a bare ASCII command via the event channel (op 0x6A)."""
    with event_connect(host) as s:
        s.sendall(build_event_frame(0x6A, text.encode()))
        s.settimeout(3.0)
        try:
            while True:
                frame = read_event_frame(s)
                if not frame:
                    break
                op, status, payload = frame
                print(_fmt_event(op, status, payload))
                if op == 0x6A:  # ack received
                    break
        except TimeoutError:
            pass


def event_query(host: str, text: str) -> None:
    """READ_* query via op 0xD0 (e.g. READ_fwdownload_xml)."""
    with event_connect(host) as s:
        s.sendall(build_event_frame(0xD0, text.encode()))
        s.settimeout(4.0)
        try:
            while True:
                frame = read_event_frame(s)
                if not frame:
                    break
                op, _status, payload = frame
                if op == 0xD0:
                    print(payload.decode("utf-8", "replace"))
                    return
        except TimeoutError:
            print("no reply")


# ---------------------------------------------------------------------------
# NV items (LibreEnv property store), reachable on event-channel op 0xD0:
#     READ_<name>            -> "<name>:<value>"
#     WRITE_<name>,<value>   -> sets it, replies with the read-back
#
# The name table below was recovered from /system/bin/LibreEnv and VERIFIED
# against a real A100: the on-flash ENV records store a 1-based UID that indexes
# exactly into this list, and 8/8 spot-checks matched the UIDs used by
# luci_service's SetEnvItemByID() calls. Order therefore matters -- do not sort.
NV_ITEMS = (
    "SpotifyEnabled",
    "ssid",
    "security",
    "passphrase",
    "netif",
    "fw_method",
    "staticip",
    "staticipaddr",
    "fw_upgrade",
    "spotify_log",
    "FriendlyName",
    "FwVersion",
    "MCUVersion",
    "CUSTVersion",
    "telnet",
    "hostpresent",
    "onetouchurl",
    "uuid",
    "AirplayPassword",
    "ddms_SSID",
    "WACMode",
    "seednv",
    "airplay",
    "DDMSOOHmode",
    "autoip",
    "CastSetup",
    "MCULatency",
    "activeinterface",
    "p2p_state",
    "ddms_BAND",
    "zoneid",
    "ddms_sp_type",
    "fw_port",
    "SP_BLOB",
    "SP_USERNAME",
    "spotifyPid",
    "factory_reset",
    "current_volume",
    "LRCK",
    "boot_after_factory",
    "WAC_SSID",
    "ACPpresent",
    "Model",
    "Manufacturer",
    "sdcard_playindex",
    "ddms_password",
    "MCLK",
    "xmodem_pkt_size",
    "AirPlayMetaData",
    "BTCLK",
    "ControllerID",
    "ControllerPublicKey",
    "HKAccessoryPassword",
    "HKAccessoryUUID",
    "ddms_channel",
    "SPT_Preset1",
    "SPT_Preset2",
    "SPT_Preset3",
    "ScheduledUpdateTime",
    "CloudLogInfo",
    "speechvolume",
    "dmrPId",
    "Location",
    "spotVol",
    "ddms_rate",
    "AcpToLS",
    "DNS",
    "BT_CONTROLLER",
    "concurrent_SSID",
    "QPlay_MID",
    "QPlay_HashKey",
    "LEDControl",
    "netmask",
    "gateway",
    "primdns",
    "secdns",
    "ddms_stream_type",
    "wifiband",
    "ddmstranscode",
    "mramode",
    "LSHOST",
    "AutoWac",
    "SDDPEnable",
    "SDDPVersion",
    "SDDPType",
    "SDDPPrimaryProxy",
    "SDDPProxies",
    "SDDPDriver",
    "SDDPMaxAge",
    "SDDPConfig_URL",
    "AirPlay_TestDelay",
    "PlayerLatency",
    "BT_DeviceName",
    "SpotifyAppkey",
    "Country",
    "DLNA_ConnClosed",
    "HOST_BAUDRATE",
    "fwupdate_link",
    "fwdownload_xml",
    "AlbumArtMaxSizeKB",
    "Serial_num",
    "Model_num",
    "Hardware_version",
    "Firmware_version",
    "HTTPHost",
    "DeezerUserName",
    "DeezerUserPassword",
    "ExternalDAC",
    "TidalUserName",
    "TidalUserPassword",
    "PlayerState",
    "SpotifyDDMSMasterName",
    "LuciTcpConnectionLimit",
    "LED_RGB",
    "LED_INTENSITY",
    "LED_FLASHING",
    "LED_DEVICE",
    "LED_AMBER",
    "LED_WHITE",
    "GEN_FAV_0",
    "GEN_FAV_1",
    "GEN_FAV_2",
    "GEN_FAV_3",
    "GEN_FAV_4",
    "GEN_FAV_5",
    "GEN_FAV_6",
    "GEN_FAV_7",
    "GEN_FAV_8",
    "GEN_FAV_9",
    "LastPlayedURL",
    "disable_eth_phy",
    "append_macid",
    "HN_SSID_0",
    "AirableBaseURL",
    "AirableSecret",
    "AirableAuth",
    "AirableLanguage",
    "RoonOutputType",
    "current_mute",
    "CloudEndPointUrl",
    "HN_PASSPHRASE_0",
    "QobuzUserName",
    "QobuzUserPassword",
    "NapsterUserName",
    "NapsterUserPassword",
    "HiResUserName",
    "HiResUserPassword",
    "CloudProductKey",
    "MultipleSSIDEnabled",
    "HostUiEnabled",
    "DirectPrefixSet",
    "Scene_Name",
    "HostIP",
    "I2S_Master",
    "RedirectionUrl",
    "NTP_Server",
    "eastechcust",
    "DMRDisable",
    "BT_BAUDRATE",
    "UART_Mode",
    "psm_timer_based_triggers",
    "psm_no_nw_pb_inact_tmr",
    "psm_hn_nw_pb_inact_tmr",
    "psm_nw_stdby_inact_tmr",
    "OOH_SSID",
    "3gbridging",
    "CloudServer",
    "CloudPort",
    "fwupdate_success",
    "IPADDR",
    "REMOTE_BD_ADDR",
    "otaupdate_link",
    "cast_version",
    "spdif",
    "vTunerLoginURL",
    "vTunerLoginURLBackUp",
    "vTunerSearchURL",
    "vTunerSearchURLBackup",
    "BlowfishKey",
    "BlowfishInitialVector",
    "vTunerTokenURL",
    "vTunerTokenURLBackUp",
    "SoundQuality",
    "CustomerId",
    "WEPKeyIndex",
    "SPKFWVersion",
    "SingleSpeaker",
    "ProductName",
    "FactoryCountry",
    "GoogleCast",
    "ShareTimeout",
    "TimeZoneCast",
    "CastTOS",
    "outputfs",
    "HardwarePlatform",
    "Brand",
    "ProductType",
    "ProductReleaseTrack",
    "ProductBuildType",
    "ProductBuildUser",
    "appsourcelist",
    "StereoPairMode",
    "StereoPairTimeOut",
    "CastSSIDSuffix",
    "SlaveFollowMasterVol",
    "SMUserName",
    "SMUserPassword",
    "SAModeUnicast",
    "IsFDR",
    "RebootSource",
    "SpotifyClientId",
    "NG_Attack",
    "NG_Release",
    "NG_Hold_time_Down",
    "NG_Hold_time_Up",
    "NG_Lower_Threshold",
    "NG_Upper_Threshold",
    "UIcount",
    "LUCIBLE",
    "CRCenable",
    "USBalbumart",
    "BTAAC",
    "AlexaRefreshToken",
    "AlexaClientID",
    "privacyMode",
    "AlexaProductID",
    "CurrentLocale",
    "Endpointurl",
    "MCG_state",
    "InputSharing",
    "LSMSUUID",
    "BT_Delay",
    "AP_ProductType",
    "LipSync_SSID",
    "LipSync_DC",
    "HostAP_ssid",
    "HostAP_security",
    "HostAP_passphrase",
    "Lipsync_state",
    "Lipsync_channel",
    "antdiv",
    "SpotifyProductID",
    "SpotifySpeakerType",
    "GCASTVersion",
    "append_btmacid",
    "Language",
    "CastPlayerLatency",
    "antennatype",
    "RoonEnable",
    "DOP_ENABLED",
    "RoonMRALatency",
    "AlexaDTID",
    "MRMPlayOffset",
    "mrmnominaldriftppm",
    "EnvItems",
)

# Observed actually populated on a live A100 (firmware LS9 3957 / MCU 44).
# These are the ones most likely to return a value rather than an empty string.
NV_LIVE = (
    "ssid",
    "security",
    "FriendlyName",
    "FwVersion",
    "MCUVersion",
    "uuid",
    "WACMode",
    "zoneid",
    "ddms_sp_type",
    "current_volume",
    "boot_after_factory",
    "Location",
    "DNS",
    "concurrent_SSID",
    "BT_DeviceName",
    "HTTPHost",
    "LastPlayedURL",
    "Scene_Name",
    "HostIP",
    "IPADDR",
    "REMOTE_BD_ADDR",
    "cast_version",
    "GoogleCast",
    "CastTOS",
)

# Default read set: useful for monitoring, safe (read-only).
NV_MONITOR = (
    # firmware / versions
    "Firmware_version",
    "FwVersion",
    "MCUVersion",
    "CUSTVersion",
    "SPKFWVersion",
    "GCASTVersion",
    "cast_version",
    "Hardware_version",
    "HardwarePlatform",
    # identity
    "Serial_num",
    "Model",
    "Model_num",
    "Manufacturer",
    "Brand",
    "ProductName",
    "ProductType",
    "SingleSpeaker",
    "FriendlyName",
    "uuid",
    # playback / state
    "current_volume",
    "current_mute",
    "PlayerState",
    "RebootSource",
    "LastPlayedURL",
    "Scene_Name",
    # multiroom / DDMS
    "StereoPairMode",
    "StereoPairTimeOut",
    "ddms_sp_type",
    "ddms_channel",
    "ddms_SSID",
    "ddms_BAND",
    "ddms_rate",
    "zoneid",
    "mramode",
    "hostpresent",
    # network
    "ssid",
    "netif",
    "activeinterface",
    "wifiband",
    "IPADDR",
    "staticip",
    "netmask",
    "gateway",
    "primdns",
    "secdns",
    "Country",
    # feature flags
    "telnet",
    "GoogleCast",
    "CastTOS",
    "SpotifyEnabled",
    "airplay",
    "WACMode",
    "RoonEnable",
    "DMRDisable",
    "SDDPEnable",
    # LEDs / audio
    "LEDControl",
    "LED_INTENSITY",
    "LED_RGB",
    "SoundQuality",
    "outputfs",
    "PlayerLatency",
    "MCULatency",
    # presets / favourites
    "GEN_FAV_0",
    "GEN_FAV_1",
    "GEN_FAV_2",
    "GEN_FAV_3",
    "GEN_FAV_4",
    "SPT_Preset1",
    "SPT_Preset2",
    "SPT_Preset3",
    # bluetooth
    "BT_DeviceName",
    "REMOTE_BD_ADDR",
    # update
    "fwdownload_xml",
    "fwupdate_link",
    "otaupdate_link",
    "ScheduledUpdateTime",
    "fwupdate_success",
)

# Values that are secrets or personally identifying. `nvdump` masks these unless
# --show-secrets is passed, so its output can be pasted into a bug report.
NV_SECRET = frozenset(
    {
        "passphrase",
        "security",
        "ssid",
        "AirplayPassword",
        "ddms_password",
        "SP_BLOB",
        "SP_USERNAME",
        "HKAccessoryPassword",
        "HKAccessoryUUID",
        "ControllerPublicKey",
        "ControllerID",
        "uuid",
        "LSMSUUID",
        "Location",
        "seednv",
        "AirableAuth",
        "AirableSecret",
        "CloudProductKey",
        "CloudLogInfo",
        "QPlay_HashKey",
        "QPlay_MID",
        "BlowfishKey",
        "BlowfishInitialVector",
        "SpotifyAppkey",
        "SpotifyClientId",
        "AlexaRefreshToken",
        "AlexaClientID",
        "DeezerUserName",
        "DeezerUserPassword",
        "TidalUserName",
        "TidalUserPassword",
        "QobuzUserName",
        "QobuzUserPassword",
        "NapsterUserName",
        "NapsterUserPassword",
        "HiResUserName",
        "HiResUserPassword",
        "SMUserName",
        "SMUserPassword",
        "HostAP_ssid",
        "HostAP_passphrase",
        "HostAP_security",
        "HN_SSID_0",
        "HN_PASSPHRASE_0",
        "WAC_SSID",
        "OOH_SSID",
        "ddms_SSID",
        "concurrent_SSID",
        "LipSync_SSID",
        "REMOTE_BD_ADDR",
        "onetouchurl",
        "LastPlayedURL",
        "IPADDR",
        "HostIP",
        "HTTPHost",
        "staticipaddr",
        "DNS",
        "primdns",
        "secdns",
        "gateway",
        "netmask",
        "CustomerId",
        "CloudEndPointUrl",
        "Endpointurl",
        "RedirectionUrl",
        "CloudServer",
        "LSHOST",
        "GEN_FAV_0",
        "GEN_FAV_1",
        "GEN_FAV_2",
        "GEN_FAV_3",
        "GEN_FAV_4",
        "GEN_FAV_5",
        "GEN_FAV_6",
        "GEN_FAV_7",
        "GEN_FAV_8",
        "GEN_FAV_9",
        "SPT_Preset1",
        "SPT_Preset2",
        "SPT_Preset3",
    }
)

# Writing these can take the speaker off the network, wipe it, or lock you out.
# `nvwrite` refuses them unless --force is given.
NV_DANGEROUS = frozenset(
    {
        "ssid",
        "security",
        "passphrase",
        "netif",
        "staticip",
        "staticipaddr",
        "netmask",
        "gateway",
        "primdns",
        "secdns",
        "DNS",
        "autoip",
        "factory_reset",
        "boot_after_factory",
        "IsFDR",
        "disable_eth_phy",
        "fw_method",
        "fw_upgrade",
        "fw_port",
        "seednv",
        "uuid",
        "HostAP_ssid",
        "HostAP_passphrase",
        "HostAP_security",
        "append_macid",
        "append_btmacid",
        "antdiv",
        "antennatype",
        "MCLK",
        "LRCK",
        "BTCLK",
        "HOST_BAUDRATE",
        "BT_BAUDRATE",
        "UART_Mode",
        "xmodem_pkt_size",
        "I2S_Master",
        "ExternalDAC",
    }
)


def nv_valid(name: str) -> bool:
    return name in NV_ITEMS


def nv_suggest(name: str) -> list:
    """Case-insensitive / substring suggestions for a mistyped NV name."""
    low = name.lower()
    exact = [n for n in NV_ITEMS if n.lower() == low]
    if exact:
        return exact
    return [n for n in NV_ITEMS if low in n.lower()][:8]


def nv_mask(name: str, value: str, show_secrets: bool = False) -> str:
    if value and name in NV_SECRET and not show_secrets:
        return f"<redacted {len(value)} chars>"
    return value


def _nv_read_one(sock: socket.socket, name: str, timeout: float = 2.0):
    """Send READ_<name> on an open event socket; return the value or None."""
    sock.sendall(build_event_frame(0xD0, ("READ_" + name).encode()))
    sock.settimeout(timeout)
    try:
        while True:
            frame = read_event_frame(sock)
            if not frame:
                return None
            op, status, payload = frame
            if op != 0xD0:
                continue
            if status == 2:  # firmware signals failure with status byte 2
                return None
            text = payload.decode("utf-8", "replace")
            prefix = name + ":"
            return text[len(prefix) :] if text.startswith(prefix) else text
    except TimeoutError:
        return None


def nvdump(host: str, names, show_secrets: bool = False, only_set: bool = False):
    """READ_ each NV item over one connection; print name -> value.

    Secrets are masked unless show_secrets, so output is safe to paste into an
    issue. only_set hides empty/unsupported items.
    """
    shown = populated = 0
    with event_connect(host) as s:
        for name in names:
            val = _nv_read_one(s, name)
            if val:
                populated += 1
            elif only_set:
                continue
            shown += 1
            print(f"{name:26} {nv_mask(name, val, show_secrets) if val else '(empty)'}")
    print(f"\n# {populated} populated / {shown} shown / {len(tuple(names))} queried")


def nvwrite(host: str, name: str, value: str, force: bool = False) -> None:
    """WRITE_<name>,<value> on op 0xD0, then show the read-back.

    The firmware splits the payload on the FIRST comma, so values may contain
    commas. Some items only take effect after a service or speaker restart, so a
    successful read-back is not proof the change is live.
    """
    if not nv_valid(name):
        print(f"unknown NV item {name!r}")
        alts = nv_suggest(name)
        if alts:
            print("did you mean:", ", ".join(alts))
        return
    if name in NV_DANGEROUS and not force:
        print(
            f"refusing to write {name!r}: this can take the speaker off the\n"
            f"network, wipe it, or lock you out. Re-run with --force if you\n"
            f"really mean it."
        )
        return
    with event_connect(host) as s:
        before = _nv_read_one(s, name)
        print(f"before: {name} = {nv_mask(name, before) if before else '(empty)'}")
        s.sendall(build_event_frame(0xD0, f"WRITE_{name},{value}".encode()))
        s.settimeout(3.0)
        try:
            while True:
                frame = read_event_frame(s)
                if not frame:
                    break
                op, status, payload = frame
                if op == 0xD0:
                    if status == 2:
                        print("write FAILED (status 2)")
                    else:
                        print("reply :", payload.decode("utf-8", "replace"))
                    break
        except TimeoutError:
            print("no reply to write")
        after = _nv_read_one(s, name)
        print(f"after : {name} = {nv_mask(name, after) if after else '(empty)'}")
        if after == before:
            print("note: value unchanged — may need a restart, or is read-only")


def event_op(host: str, op: int, payload: bytes = b"", listen: float = 2.5) -> None:
    """Send a raw LUCI op on the event channel and print any frames that arrive.

    For the firmware-recovered ops (reboot 0x72, net-standby 0x16/0x17,
    fw-update 0xEC, ...); see docs/PROTOCOL.md. Firmware-derived, so verify the
    effect on your own speaker.
    """
    with event_connect(host) as s:
        s.sendall(build_event_frame(op, payload))
        s.settimeout(listen)
        try:
            while True:
                frame = read_event_frame(s)
                if not frame:
                    break
                print(_fmt_event(*frame))
        except TimeoutError:
            pass


def _fmt_event(op: int, status: int, payload: bytes) -> str:
    label = {
        0x03: "handshake",
        0x16: "net-standby-start",
        0x17: "net-standby-end",
        0x18: "standby-status",
        0x40: "volume",
        0x48: "wifi-scan",
        0x49: "wifi-scan-results",
        0x64: "ddms",
        0x67: "channel-status",
        0x6C: "stereopair-mode",
        0x6A: "ascii-ack",
        0x70: "mirror",
        0x72: "reboot-request",
        0x97: "rssi",
        0xD0: "query-reply",
        0xE8: "battery-power",
        0xEC: "speaker-fw-update",
    }.get(op, f"op 0x{op:02x}")
    if op == 0x70 and len(payload) >= 5:
        length = struct.unpack(">H", payload[0:2])[0]
        body = payload[2 : 2 + length]
        group = struct.unpack(">H", body[0:2])[0]
        cmd = body[2]
        data = body[3:]
        return f"[mirror] group={group} cmd=0x{cmd:02x}" + (
            f" value={_decode(data)!r}" if data else " (get)"
        )
    text = payload.decode("utf-8", "replace") if payload else ""
    return f"[{label}] status={status}" + (f" {text}" if text else "")


def watch(host: str) -> None:
    """Subscribe to the push/event channel and print every state change."""
    print("watching push events on port 7777 (Ctrl-C to stop)...")
    with event_connect(host) as s:
        s.sendall(build_event_frame(0x40, version=0x01))  # like the app does
        s.settimeout(None)
        while True:
            frame = read_event_frame(s)
            if not frame:
                print("connection closed")
                return
            print(_fmt_event(*frame))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    host, verb, *rest = sys.argv[1:]

    if verb == "status":
        print(json.dumps(get_status(host), indent=2, ensure_ascii=False))
    elif verb == "volume":
        send_cmd(host, f"volume {int(rest[0])}")
    elif verb in ("volup", "voldown", "play", "pause", "standby", "power"):
        send_cmd(host, verb if verb != "standby" else "timerstandby")
    elif verb == "source":
        send_cmd(host, f"source {rest[0]}")
    elif verb == "aux-trigger":
        # wire command is "disable auto aux" -> inverted
        send_bin(host, 2, 0x9E, 0 if int(rest[0], 0) else 1)
    elif verb == "restart":
        # power action: value 2 = reboot (speaker drops off the network briefly)
        send_bin(host, 2, 0x4D, 2)
    elif verb == "srcid":
        # select source by numeric id (19 = Bluetooth, 25 = Analog IN)
        send_bin(host, 2, 0x03, int(rest[0], 0))
    elif verb == "binvolume":
        send_bin(host, 2, 0x2A, int(rest[0], 0))
    elif verb in TOGGLES:
        group, cmd = TOGGLES[verb]
        send_bin(host, group, cmd, int(rest[0], 0))
    elif verb == "bassboost":
        send_cmd(host, f"basssboost {int(rest[0])}")
    elif verb == "url":
        send_cmd(host, f"url {rest[0]}")
    elif verb == "maxvolume":
        send_cmd(host, f"maxvolume {int(rest[0])}")
    elif verb == "channel":
        event_ascii(
            host,
            {"stereo": "SETSTEREO", "left": "SETLEFT", "right": "SETRIGHT"}[
                rest[0].lower()
            ],
        )
    elif verb == "unpair":
        # SETFREE is handled by the ddms op 0x64, NOT by 0x6A (which only knows
        # SETLEFT/SETSTEREO/SETRIGHT). Payload length is checked exactly.
        event_op(host, 0x64, b"SETFREE")
    elif verb == "ddms":
        # raw ddms verb: SETMASTER|SETSLAVE|SETFREE|JOINTO|JOINALL|JOINNEXT|
        # JOINNEXTLEFT|JOINNEXTRIGHT|DROPALL|DROPME
        event_op(host, 0x64, rest[0].upper().encode())
    elif verb == "pairmode":
        # MASTERLEFT|MASTERRIGHT|SLAVELEFT|SLAVERIGHT via 0x6C
        event_op(host, 0x6C, rest[0].upper().encode())
    elif verb == "reboot-luci":
        event_op(host, 0x72)  # RebootRequest (LUCI path; drops off the network)
    elif verb == "net-standby":
        # on -> NET_STANDBY_START (0x16), off -> NET_STANDBY_END (0x17)
        event_op(host, 0x16 if rest[0].lower() in ("on", "1", "start") else 0x17)
    elif verb == "standby-status":
        event_op(host, 0x18)  # STANDBY_STATUS
    elif verb == "wifi-scan":
        event_op(host, 0x48)  # TriggerWifiScan; results push on 0x49
    elif verb == "fw-update":
        # SPEAKER_FW_UPDATE (0xEC): the app's "update now" trigger. Only useful
        # if a firmware is actually published; the update server was empty.
        event_op(host, 0xEC)
    elif verb == "event-op":
        # generic: event-op <op-hex> [ascii-payload]
        event_op(host, int(rest[0], 0), rest[1].encode() if len(rest) > 1 else b"")
    elif verb == "cmd":
        send_cmd(host, rest[0])
    elif verb == "raw":
        send_raw(host, rest[0])
    elif verb == "get":
        group, cmd = int(rest[0], 0), int(rest[1], 0)
        print(request(host, group, cmd, cmd + 1))
    elif verb == "bin":
        send_bin(host, int(rest[0], 0), int(rest[1], 0), int(rest[2], 0))
    elif verb == "readq":
        event_query(host, rest[0])
    elif verb == "nv":
        # READ_<name>, e.g. `nv FwVersion`
        name = rest[0]
        if not nv_valid(name):
            print(f"unknown NV item {name!r}")
            alts = nv_suggest(name)
            if alts:
                print("did you mean:", ", ".join(alts))
            return 1
        event_query(host, "READ_" + name)
    elif verb == "nvwrite":
        nvwrite(host, rest[0], rest[1], force="--force" in rest)
    elif verb == "nvdump":
        show = "--show-secrets" in rest
        only_set = "--set" in rest
        sel = [a for a in rest if not a.startswith("--")]
        group = sel[0].lower() if sel else ""
        if group == "all":
            names = NV_ITEMS
        elif group == "live":
            names = NV_LIVE
        elif sel:
            names = sel
        else:
            names = NV_MONITOR
        nvdump(host, names, show_secrets=show, only_set=only_set)
    elif verb == "kleernet":
        kleernet_report(host)
    elif verb == "kleernet-unpair":
        # group 3 / 0x05 + partner serial = UNPAIR (packet-capture verified:
        # paired[] emptied ~4 s later). NOT the pair command.
        sn = rest[0].strip().upper()
        send_frame(host, 3, 0x05, sn.encode("ascii"), expect=(0x04,), timeout=2.0)
        print("takes a few seconds; poll with: kleernet")
    elif verb == "kleernet-pairmode":
        # group 3 / 0x01, empty payload, no reply. The app sends this after an
        # unpair; the partner reappeared ~11 s later with no further traffic.
        # no reply is expected; "no ack" below is the normal outcome
        send_frame(host, 3, 0x01, expect=(0x01, 0x02))
        print("the bind happens over the Kleernet radio; poll with: kleernet")
    elif verb == "scan":
        # scan <group> [start] [end] --i-understand
        group = int(rest[0], 0) if rest else 3
        sel = [a for a in rest if not a.startswith("--")]
        start = int(sel[1], 0) if len(sel) > 1 else 0x00
        end = int(sel[2], 0) if len(sel) > 2 else 0xFF
        if "--i-understand" not in rest:
            print(
                "scan sweeps a command range with empty-payload frames.\n"
                "Known sets/actions are skipped, but UNKNOWN commands may still\n"
                "be sets or actions on the ATMEL MCU — there is a real chance of\n"
                "changing a setting or triggering something.\n\n"
                "Safer first: run `watch` and poke the StudioART app instead.\n\n"
                f"To proceed: {sys.argv[0]} {host} scan {group} "
                f"{start:#04x} {end:#04x} --i-understand"
            )
            return 1
        scan(host, group, start, end)
    elif verb == "nvlist":
        # offline: print the known NV names (no device needed)
        pat = rest[0].lower() if rest else ""
        hits = [n for n in NV_ITEMS if pat in n.lower()]
        for i, n in enumerate(NV_ITEMS):
            if n in hits:
                print(f"  uid {i + 1:>3}  {n}")
        print(f"# {len(hits)} of {len(NV_ITEMS)} names")
    elif verb == "watch":
        watch(host)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
