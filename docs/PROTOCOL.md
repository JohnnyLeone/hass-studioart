# The STUDIOART protocol (reverse-engineered)

This document is the developer/tinkerer reference for the Revox STUDIOART
network protocol as used by the [hass-studioart](../README.md) integration.
Everything below was verified against a packet capture of the StudioART app
controlling an A100 (firmware `LS9 V3957 / Controller V44`) unless marked
otherwise. "Verified on a live speaker" means the command was replayed against
real hardware and the effect confirmed.

The speaker exposes two TCP ports:

| Port | Purpose |
|---|---|
| 50007 | Control: binary request/response **and** ASCII commands, on the same socket |
| 7777 | Event/push channel: state pushes, command mirror, app-style ASCII ops |

## Port 50007 — control

Carries **two protocols at once**:

### 1. Binary request/response (status + settings)

Length-prefixed frames:

```
[uint16 length][uint16 group][uint8 cmd][payload...]
```

`length` counts everything after itself (`2 + 1 + len(payload)`). `group` is a
namespace (`2` = device/settings, `3` = multi-room/Kleernet). Settings follow
a triplet: **get = N, reply = N+1, set = N+2**; a set is acknowledged with the
same reply cmd `N+1` carrying the new value. Payloads are a single byte or a
UTF-8 JSON object.

| Group | Get | Reply | Set | Meaning | Payload / notes |
|---|---|---|---|---|---|
| 2 | — | — | `0x03` | **Select source** ✓ | numeric id, device-verified: `19` = Bluetooth, `25` = Analog IN. Display-only ids: `1` = an active **AirPlay** session, `4` = an active **Spotify Connect** session — both activate themselves when a client connects and cannot be selected. Not to be confused with the *group 3* `0x03` multi-room get |
| 2 | `0x28`* | `0x29` | `0x2A` | **Volume** ✓ | 0-100; `0x29` is pushed on every change, and the speaker echoes a console frame (`group 0x00FF`, cmd `0xFF`) with `{"cmd":"set volume:NN OK"}` |
| 2 | `0x30` | `0x31` | — | Preset list(?) | returned `[]` (all presets empty on the test device) |
| 2 | `0x34` | `0x35` | `0x36` | **Loudness** ✓ | `0/1` — verified on a live speaker |
| 2 | `0x37` | `0x38` | — | **Device status** | JSON: `SSID, MAC, RSSI, IP, SN, LS9, Kleernet, Controler, Name, Battery, STBY, volume, Brightness, UpdateMode, UpdateState, mcuType, AutoPowerOn, PowerOnSrc, netstate`. `RSSI` is a quality code, higher = worse: `2` = Good, `3` = Bad, `4` = Very bad (device-verified; `1` = Very good inferred) |
| 2 | `0x33` | ? | — | **Play state read** | sent by the app on connect; the state is pushed as event op `0x33` |
| 2 | `0x3C` | `0x3D` | — | **Playback** | JSON: `{"source":4,"state":1,"volume":22,"url":"","title":"…","albumUrl":"https://…"}` — `state`: `0` = stopped, `1` = playing (paused also reports `0`; "paused" only exists in the event pushes). `title`/`albumUrl` are present while a track is loaded |
| 2 | `0x41` | `0x42` | `0x43` | **Aux-In high sensitivity** ✓ | `0/1` — verified on a live speaker |
| 2 | `0x47` | `0x48` | — | unknown flag | value `0` in capture |
| 2 | `0x59`* | `0x5A` | `0x5B` | **Auto power on** | ack is JSON `{"AutoPowerOn":n}`; state also in device status |
| 2 | `0x60`* | `0x61` | `0x62` | **Switch L/R channel** | `0/1`; state also in multi-room `LRreverse` |
| 2 | `0x4B`* | `0x4E` | `0x4D` | **Power action** ✓ | value `2` = restart (ack `{"poweroff":1}`, speaker reboots); note: reply is `0x4E` = cmd+1 |
| 2 | `0x56`* | `0x57` | `0x58` | **Power-on source** ✓ | ack `{"PowerOnSrc":n}`; `0` = Last played, `1-5` = Presets, `6` = Bluetooth, `7` = Analog IN — all confirmed by cycling the app menu |
| 2 | `0x8D` | `0x8E` | `0x8F`* | **Standby timer** | JSON `{"timersty":n}` (minutes; read when the app opens the power menu). The set for Immediately/15/30/45/60 min is inferred, not yet captured |
| 2 | `0x99`* | `0x9A` | `0x9B` | **Kleernet wireless band** ✓ | `0` = automatic, `1` = 2.4G, `2` = 5.2G, `3` = 5.8G (device-verified); state = `D83Fre` in the Kleernet JSON |
| 2 | — | `0x9D` | `0x9E` | **Disable auto aux** ✓ | `1` = Aux-In trigger **off** (inverted!) — verified on a live speaker; state = `DisAutoAux` in the Kleernet JSON |
| 3 | `0x03` | `0x04` | — | **Multi-room state** | JSON: `{"state":2,"LRreverse":0,"paired":[{"type":"A100","name":"…","ID":"…","volume":48,"channel":1,"battery":255}]}` |
| 3 | `0x56` | `0x57` | — | **Kleernet config** | JSON: `{"D83Fre":0,"DisAutoAux":0}` — `D83Fre` = wireless band, `DisAutoAux` = inverted Aux-In trigger. NB: same cmd numbers as the *group 2* power-on-source triplet — the group disambiguates |
| 3 | `0x0F` | — | — | **Check P100** ✓ | fire-and-forget probe for a *wired* P100 partner speaker (independent of Kleernet pairing); confirmed to send no reply |

`*` = inferred from the triplet pattern, not yet observed on the wire.
Beware: a first capture-only analysis mapped `0x36` to the Aux-In trigger and
`0x9E` to loudness — live testing showed it is the other way round, with `0x9E`
being the *inverted* "disable auto aux" flag. Don't trust UI-order heuristics.

Note: sending a *get* of a settings triplet makes the StudioART app (if open
and subscribed to the mirror channel) briefly flicker the corresponding toggle
— the mirror wraps the empty get frame and the app seems to misrender it. The
official app causes the same effect on other clients when it polls; it is
cosmetic and the device state is untouched. To keep the app usable alongside
Home Assistant, the integration reads the loudness/high-sensitivity triplets
at most once a minute and relies on mirror pushes in between.

### 2. ASCII "telnet" control (valid from A100 firmware V41+, S100 V63+)

Send `cmd <text>\r\n` to port 50007:

```
cmd volume 0-100        cmd volup            cmd voldown
cmd maxvolume 1-100     cmd play             cmd pause
cmd source preset 0..4  cmd source BT        cmd source aux
cmd url <URL>           cmd loudness         cmd basssboost 0,1
cmd timerstandby        cmd power
```

(S100 also: `cmd source TV|hdmi1|hdmi2|hdmi3`. Note the documented
bass-boost keyword really is spelled `basssboost`, three s.)

## Port 7777 — event/push channel

Message-framed, asymmetric headers:

```
client -> speaker:  [00 00 VV][OP][00 00 00 00][uint16 len LE][payload]
speaker -> client:  [00 00 VV 00][OP][ST][uint16 crc][uint16 len BE][payload]
```

`VV` is `0x02` for most ops (`0x01` for the legacy volume query `0x40`). The
client sends the checksum bytes as zeros — the speaker accepts that; replies
carry a 16-bit checksum which can be ignored.

| Op | Direction | Meaning |
|---|---|---|
| `0x03` | c→s | subscribe/handshake (empty payload) |
| `0x0A` / `0x32` | s→c | source changed push — payload is the ASCII source id (e.g. `"19"`) |
| `0x2A` / `0x2D` | s→c | **"PlayView" push**: JSON with now-playing metadata — `TrackName`, `Artist`, `Album`, `CoverArtUrl`, `TotalTime` (ms), `PlayState`, `Current Source`, `Shuffle`, `Repeat`, `PlayUrl` (sent twice, once per op) |
| `0x31` | s→c | playback position push in ms, ~1/second while playing |
| `0x33` | s→c | play-state push — **ASCII `0` = playing/active, `2` = paused** (NB: a *different* enum than the playback JSON's `state`!). Fires for AirPlay/Spotify too, enabling instant state in HA |
| `0x40` | c→s (`VV=0x01`) | volume query — reply payload is the ASCII volume; also pushed on volume changes |
| `0x46` | s→c | `SPEAKER_ACTIVE,<source id>` push |
| `0x67` | s→c | multi-room channel status push: `FREE,STEREO,<concurrent-SSID>` (pair-state, channel) |
| `0x6A` | c→s | send a bare ASCII command — **this is how the app sends `SETSTEREO` / `SETLEFT` / `SETRIGHT`** |
| `0x70` | s→c | **mirror push**: wraps every binary frame the speaker *receives* on port 50007, from any client — sets carry the new value, so subscribers learn about every change instantly |
| `0xD0` | c→s | ASCII query, e.g. `READ_fwdownload_xml` → `fwdownload_xml:http://update.revox.de/Studioproducts/A100ATMEL/fw_update.xml` |
| `0xD1` | s→c | Bluetooth event push, e.g. `btdisconnect` |
| `0xE6` | s→c | sample rate push when a stream starts, e.g. `48000` |
| `0xEE` | s→c | empty stream-start marker |

The integration keeps a persistent subscription on this channel: when you flip
a toggle in the StudioART app, the mirrored set frame updates the Home
Assistant entity immediately, and a debounced poll picks up anything that
can't be decoded from the mirror alone.

## Discovery

The speaker advertises `_http._tcp` (:80), `_spotify-connect._tcp` (:9095),
`_raop._tcp` and `_airplay._tcp` (:7000) via mDNS, and answers an
SSDP-like "LSSDP" probe on UDP 1800 (banner includes `DeviceName`,
`FWVERSION`, `TCPPORT:2020`, `PORT:7777`, `SOURCE_LIST:LS9::f77fffff`).
The config flow uses the AirPlay/RAOP records (`am=RevoxA100`,
`model=RevoxA100`) to auto-discover, and you can also add by IP. The config
entry is keyed to the device serial (`SN`), so DHCP address changes are
followed automatically on rediscovery.

## How the entities map to the protocol

| Entity | Backing command | Confidence |
|---|---|---|
| `media_player` | binary + ASCII `cmd …` commands, status reads and pushes | High |
| `switch` Aux-In trigger | binary set `0x9E` **inverted** ("disable auto aux"), state = Kleernet `DisAutoAux` | **Verified on a live speaker** |
| `switch` Aux-In trigger high sensitivity | binary get `0x41` / set `0x43` | **Verified on a live speaker** |
| `switch` Loudness | binary get `0x34` / set `0x36` | **Verified on a live speaker** |
| `switch` Switch L/R channel | binary set `0x62`, state = `LRreverse` | **Confirmed on the wire** |
| `switch` Auto power on | binary set `0x5B` | **Confirmed on the wire** |
| `switch` Bass boost | ASCII `cmd basssboost 0/1` | Documented (optimistic, not reported back) |
| `select` Multi-room speaker setting | `SETSTEREO/SETLEFT/SETRIGHT` via event channel | **Confirmed on the wire**, state pushed back |
| `select` Power-on source | binary set `0x58`, state = `PowerOnSrc` | **Verified on a live speaker** (all indices) |
| `select` Kleernet wireless band | binary set `0x9B`, state = Kleernet `D83Fre` | **Verified on a live speaker** |
| `button` Restart | binary `0x4D` value `2` | **Confirmed on the wire** |
| `button` Check P100 | binary group 3 / `0x0F` | **Confirmed on the wire** |
| `sensor` Paired speaker (+ battery) | multi-room JSON `paired[]` | **Confirmed on the wire** |
| `number` Max volume limit | ASCII `cmd maxvolume N` | Documented (optimistic, not reported back) |
| `sensor` Battery, Wi-Fi, IP, brightness | binary status reads | **Confirmed on the wire** |
| `binary_sensor` Battery charging (chief + paired) | battery byte `254` in status reads | **Verified on a live speaker** |

Battery byte encoding: `0-100` = state of charge, `254` = charging (the SoC
is not reported while charging), `255` = fully charged / on mains.

The `STBY` flag in the device status is `1` even while actively playing, so
it cannot indicate a power state — this is why the integration has no power
switch and derives playing/idle from the playback state instead.

## The CLI — poke the speaker without Home Assistant

[`tools/revox_cli.py`](../tools/revox_cli.py) is a dependency-free script
speaking the same protocol:

```bash
python3 tools/revox_cli.py 192.168.42.163 status
python3 tools/revox_cli.py 192.168.42.163 volume 40
python3 tools/revox_cli.py 192.168.42.163 source aux
python3 tools/revox_cli.py 192.168.42.163 loudness 1   # binary 0x36
python3 tools/revox_cli.py 192.168.42.163 aux-trigger 0  # 0x9E, inversion handled
python3 tools/revox_cli.py 192.168.42.163 channel left     # SETLEFT via port 7777
python3 tools/revox_cli.py 192.168.42.163 watch            # live push events
python3 tools/revox_cli.py 192.168.42.163 readq READ_fwdownload_xml
# poke an unknown command:
python3 tools/revox_cli.py 192.168.42.163 get 2 0x47
python3 tools/revox_cli.py 192.168.42.163 bin 2 0x36 1
```

`watch` is the best tool for mapping the remaining unknowns: it decodes the
mirror pushes, so flip things in the StudioART app and read off the
`group/cmd/value` that each UI element sends.

## Still unmapped — contributions welcome

- **Numeric source ids** beyond `19` (Bluetooth), `25` (Analog IN) and `1`
  (AirPlay session): the ids behind the Presets, iRadio, Podcasts, Server,
  Spotify, TIDAL and Deezer tiles are unknown. Tap tiles in the app while
  running `watch` (look for mirrored `group=2 cmd=0x03` sets) and report back.
- `group 2, 0x30→0x31` (returns `[]`) and `0x47→0x48` (returns `0`) — read by
  the app on connect, meaning unknown (`0x30` is possibly the preset list).
- Standby timer **set** (power menu: Immediately/15/30/45/60 min) — the read is
  `group 2, 0x8D` (`{"timersty":n}`), the set is presumably `0x8F` but has not
  been captured yet.
- Other power-action values of `group 2, 0x4D` (value `2` = restart is
  confirmed; `Immediately` standby may be another value of the same command).
- Pair/unpair flow ("Pair/Unpair Speaker → START") — not captured yet.
- Firmware update trigger (the app reads the update XML URL via
  `READ_fwdownload_xml`; the XML was empty at the time of writing).

Findings are best reported as an
[issue](https://github.com/JohnnyLeone/hass-studioart/issues) with the
`watch` output attached.
