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
| 2 | `0x33` | — | — | **Play state read** | sent by the app on connect; no reply on port 50007 (the state is pushed as event op `0x33`) |
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
| 3 | `0x01` | — | — | **Enter Kleernet pairing mode** (probable) | empty payload, never answers. Sent by the app after an unpair; the partner reappeared ~11 s later with no further network traffic |
| 3 | `0x03` | `0x04` | `0x05` | **Multi-room state / UNPAIR a speaker** ✓ | get returns JSON `{"state":2,"LRreverse":0,"paired":[{"type":"A100","name":"…","ID":"…","volume":48,"channel":1,"battery":255}]}`. The **set `0x05` UNPAIRS** the partner whose **serial number** is given as bare ASCII (e.g. `SAAD11958`) — packet-capture verified, `paired[]` empties ~4 s later. It does *not* pair. A partner's `volume` follows the chief's volume (it lags by one poll during a ramp) |
| 3 | `0x06` | `0x07` | `0x08` | unknown setting | `{"value":n,"ID":""}` — read `6` on an A100 with a partner bound. Discovered by scanning. **Not pairing-related**: unchanged across a full unpair/re-pair cycle |
| 3 | `0x09` | `0x0A` | `0x0B` | unknown setting | `{"value":n,"ID":""}` — read `0` |
| 3 | `0x0C` | `0x0D` | `0x0E` | unknown setting | `{"value":n,"ID":""}` — read `0` |
| 3 | `0x10` | `0x11` | `0x12` | unknown setting | `{"value":n,"ID":""}` — read `1` |
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

The status byte `ST` (consistent across every capture): `0` = unsolicited
push, `1` = reply to a request (e.g. the `0x03` handshake, the `0x40` volume
query, the empty ack of a `0x6A` send), `2` = error / not supported.

| Op | Direction | Meaning |
|---|---|---|
| `0x03` | c→s | subscribe/handshake (empty payload) |
| `0x0A` / `0x32` | s→c | source changed push — payload is the ASCII source id (e.g. `"19"`). `0x32` first sends a transient **`"0"`** at the start of a source switch or an AirPlay session, ~0.1–3 s before the real id; the playback JSON never reports `0`, so ignore it |
| `0x2A` / `0x2D` | s→c | **"PlayView" push**: `{"CMD ID":3,"Title":"PlayView","Window CONTENTS":{…}}` with now-playing metadata — `TrackName`, `Artist`, `Album`, `Genre`, `CoverArtUrl`, `TotalTime` (ms), `Current_time` (`-1`), `PlayState`, `Current Source`, `Shuffle`, `Repeat`, `PlayUrl` (e.g. `spotify:track:…`), capability flags `Next`/`Prev`/`Seek`, and `BitDepth`/`SampleRate`/`BitRate`/`Mime`. Sent twice, once per op, in bursts of 2–3 on every play-state change |
| `0x31` | s→c | playback position push in ms, ~1/second while playing |
| `0x33` | s→c | play-state push — **ASCII `0` = playing/active, `2` = paused** (NB: a *different* enum than the playback JSON's `state`!). Fires for AirPlay/Spotify too, enabling instant state in HA. `1` appears once at the start of an AirPlay session (together with the source-`0` push, ~4 s before `0`) — a transitional "starting" value, not a state |
| `0x40` | c→s (`VV=0x01`) | volume query — reply payload is the ASCII volume; also pushed on volume changes. Volume changed on an **AirPlay sender** arrives *only* as these pushes (no mirrored `0x2A` set), in steps of ~6 during a ramp; the playback JSON trails by ~0.2–0.5 s |
| `0x46` | s→c | `SPEAKER_ACTIVE,<source id>` push |
| `0x67` | s→c | DDMS / channel status: `FREE,STEREO,<concurrent-SSID>` (DDMS state, channel). **Not a change event**: the speaker sends it ~0.1 s after *every* group 3 / `0x03` get from any client (161 of 161 times across the captures) and in answer to `SETSTEREO`/`SETLEFT`/`SETRIGHT`. Only a changed value carries information |
| `0x6A` | c→s | send a bare ASCII command — **this is how the app sends `SETSTEREO` / `SETLEFT` / `SETRIGHT`** |
| `0x70` | s→c | **mirror push**: wraps every binary frame the speaker *receives* on port 50007, from any client — sets carry the new value, so subscribers learn about every change instantly. Gets are mirrored too, **including the subscriber's own polls** — a client that treats every mirror frame as "something changed, re-poll" polls itself in a loop |
| `0xD0` | c→s | ASCII query, e.g. `READ_fwdownload_xml` → `fwdownload_xml:http://update.revox.de/Studioproducts/A100ATMEL/fw_update.xml` |
| `0xD1` | s→c | Bluetooth event push, e.g. `btdisconnect` |
| `0xDB` | c→s | `zone volume control` — the app sends it empty when opening the speaker settings; the speaker answers with status `2` (not supported in this setup) |
| `0xE6` | s→c | sample rate push when a stream starts, e.g. `48000` |
| `0xEE` | s→c | empty stream-start marker |

The integration keeps a persistent subscription on this channel: when you flip
a toggle in the StudioART app, the mirrored set frame updates the Home
Assistant entity immediately, and a debounced poll picks up anything that
can't be decoded from the mirror alone.

### The port-7777 protocol is Libre "LUCI"

The network/streaming board inside every STUDIOART speaker is a **Libre
Wireless Technologies (LWT) "LibreSync"** Chromecast-built-in module, and the
port-7777 event channel is Libre's **LUCI** protocol. The daemon that serves
it is `/system/bin/luci_service`, recovered from the LS9 firmware image
(`fw/83_IMAGE_NETWORK`, build `3957` = the `LS9 V3957` in the device status).

`luci_service` carries a static **message table** (a `{name, id, …}` array in
`.data`) that maps every LUCI message name to a numeric **message id — and the
message id is exactly the `Op` byte above.** This was confirmed by eleven
independent matches against ops already verified on the wire:

| Op (verified) | LUCI name | Op (verified) | LUCI name |
|---|---|---|---|
| `0x2A` / `0x2D` PlayView | `RemoteUI` / `RemoteUIPlay` | `0x67` channel status | `ddms status` |
| `0x31` position | `Current Time` | `0x6A` bare-ASCII send | `speaker type` |
| `0x32` source | `Current Source` | `0xD0` `READ_*` query | `NV Read` |
| `0x33` play state | `Play Status` | `0xD1` bt event | `BT` |
| `0x40` volume | `volume control` | `0xE6` sample rate | `AUDIO_OUTPUT_FS` |
| `0x70` mirror | `Tunnel Data` (the mirror is LUCI "tunnelling") | | |

Two names refine the existing table: `0x46` ("SPEAKER_ACTIVE,…") is LUCI
**`host App control`**, and `0x70` is **`Tunnel Data`** — the "mirror" is the
LUCI tunnel feature (paired with `Tunnel Start` `0x6F`).

**Recovered ops (firmware-derived — the id is the wire `Op`, but each is not
yet replayed on hardware unless noted).** Only ids ≤ 255 can be a single-byte
`Op`; the table also contains internal ids > 255 (Cast/AVS/debug) that are not
sent on this channel. Highlights that close open questions:

| Op | LUCI name | Use |
|---|---|---|
| `0x14` / `0x15` | `DEEPSLEEP_START` / `DEEPSLEEP_END` | deep-sleep transitions |
| `0x16` / `0x17` | `NET_STANDBY_START` / `NET_STANDBY_END` | **network standby** enter/leave |
| `0x18` | `STANDBY_STATUS` | standby state query/push |
| `0x25` | `Gracefull Shutdown` | clean shutdown |
| `0x41` `0x42` `0x44` `0x45` | `FwUpgrade`, `Firmeware_progress`, `HostImage_Ready`, `RequestForFirmwareUpgrade` | firmware-upgrade handshake |
| `0x48` / `0x49` | `TriggerWifiScan` / `GetWifiScanResults` | Wi-Fi scan |
| `0x62`–`0x69` | `ddms …` (`internal`,`rate adapt`,`ddms`,`ooh master`,`ooh slave`,`status`,`groupid`,`ssid`) | **multi-room/stereo-pair (DDMS) engine** |
| `0x6A` | `speaker type` | carries the pairing ASCII (see below) |
| `0x6C` | `StereoPair Mode` | stereo-pair mode get/set |
| `0x72` / `0x73` | `RebootRequest` / `OnReboot` | **reboot** (LUCI path; the binary `group 2 0x4D=2` is the other) |
| `0x7C` / `0x7D` | `netstatus` / `net conf` | network status/config |
| `0x7F` | `Forget network` | drop saved Wi-Fi |
| `0x8C` / `0x8D` | `WPS_STATUS` / `WPS Triggger` (sic) | WPS |
| `0x96` | `FACTORY_DEFAULT` | **factory reset** (destructive) |
| `0x97` | `RSSI` | signal query/push |
| `0xCF` | `LED Control` | brightness/LED |
| `0xD2` | `DMR Reboot` | DLNA renderer restart |
| `0xD6` `0xD7` `0xD8` `0xDD` | `ShareMode`, `PairMode`, `ddmsSlaveInformation`, `PairStatus` | **pair/unpair** state machine |
| `0xDB` / `0xDC` | `zone volume control` / `client zone volume` | per-zone volume in a group |
| `0xE7` `0xE8` | `CAST_SERIAL_NUM`, `BatteryPower` | serial / battery push |
| `0xEB` `0xEC` `0xEE` | `Localcheckupdate`, **`SPEAKER_FW_UPDATE`**, `FORCED_UPDATE` | **firmware-update trigger** (the app's "update now") |

`0xEE`: the firmware table labels `0xEE` `FORCED_UPDATE`, but the integration
has observed empty `0xEE` frames at stream start — treat `0xEE` as ambiguous
until re-checked on hardware.

**Pair/unpair grammar** (decompiled from
`LucicontrolServer::IncomingHouseKeeping`). The ASCII verbs are **split across
three different ops** — they are *not* all on `0x6A`:

| Op | LUCI name | Verbs (payload length must match exactly) |
|---|---|---|
| `0x64` | `ddms` | `SETMASTER`(9), `SETSLAVE`(8), **`SETFREE`(7)**, `JOINTO`(6), `JOINALL`(7), `JOINNEXT`(8), `JOINNEXTLEFT`(12), `JOINNEXTRIGHT`(13), `DROPALL`(7), `DROPME`(6) |
| `0x6A` | `speaker type` | `SETLEFT`(7), `SETSTEREO`(9), `SETRIGHT`(8) — the channel assignment the integration already sends |
| `0x6C` | `StereoPair Mode` | `MASTERLEFT`(10), `MASTERRIGHT`(11), `SLAVELEFT`(9), `SLAVERIGHT`(10) |

So **unpair / leave a group = `SETFREE` on op `0x64`** (`DROPALL` runs the same
code path; `DROPME` is the leave-only variant). Sending `SETFREE` on `0x6A` is
silently ignored — that op only compares the three channel verbs.

The parser checks the payload length with an **exact** comparison
(`param_5 == 7` for `SETFREE`, etc.) and logs `… invalid datalen` otherwise, so
the payload must carry the bare verb with **no NUL terminator and no CRLF**.

State machine and guards, also from the decompilation:

- Group state `0` = **free**. `SETMASTER`/`SETSLAVE` are rejected unless the
  device is free — `device is not in free state, cannot be made master/slave`.
- Channel state: `0` = stereo, `1` = left, `2` = right. While the device is in
  stereo-pair mode a channel change is refused with `device is in stereo pair
  mode. type change not allowed`.
- Every state change arms a **35-second `StateChangeTimer`**; if the transition
  does not complete the device logs `Failed to do state change in … seconds…
  free the device internally` and **falls back to `SETFREE` on its own**. So a
  half-finished pairing self-heals after ~35 s.
- `SETSTEREO` additionally emits a `0x6C` (`StereoPair Mode`) message and
  writes NV items `0x20` (speaker channel type) and `0xCA` (`StereoPairMode`).
- `0x65`/`0x66` (`ddms ooh master`/`slave`) set the DDMS multicast group to
  `239.255.255.251:3000` (NV `0x1F`); `0x68` (`ddms groupid`) writes that NV
  directly.

The resulting pair state is reported by `ddms status` (`0x67`) as one of
`FREE,` / `STEREO,` / `LEFT,` / `RIGHT,` / `MASTER,` / `SLAVE,` — which is why
the current `0x67` parser sees `FREE,STEREO,<ssid>`.

### DDMS — how a stereo pair actually forms

The LUCI ops above only *steer* the pairing; the pair itself is run by the
**DDMS** subsystem (`LSDeviceService` + `libddms_rtp.so`), which is a separate
master/slave link between the two speakers:

- **Discovery** is the SSDP-like probe already documented on UDP 1800, with
  search target `ST: urn:schemas-upnp-org:device:DDMSServer:1`. A speaker finds
  its partner by SSID or zone id (`DDMSClient::createSocket() client searching
  for ssid %s` / `for zone id %s`).
- **Transport** is then a direct TCP link: the master listens/accepts and the
  slave connects (`SO_BINDTODEVICE` to the active interface; falls back to Wi-Fi
  Direct/p2p when the master's WLAN IP isn't reachable).
- **The pairing message is a CRLF `KEY:VALUE` block** (same shape as the LSSDP
  banner), built by `DDMSMaster::setupStereoPair`:

  ```
  SPTYPEUPDATE:Y
  SPTYPE:LEFT | SPTYPE:RIGHT | SPTYPE:STEREO
  CONCOUNT:1
  IP:<master ip>
  …
  ```

  `CONCOUNT` is the connection count: `1` for a stereo pair, `32` for a
  multi-channel master, `0` to clear. `CONCOUNTUPDATE:Y` announces a change.
  Other keys in the same family: `SSID:`, `PORT:`, `State:`, `MRAMode:`,
  `DDMSConcurrentSSID:`.
- **Keepalive**: the slave expects periodic `ALIVE`/`MALIVE` messages
  (`PING`/`RECONNECT`/`CLEAR` also exist). If they stop, the slave logs
  `no ALIVE msg from master…` and **frees itself** — the timeout is NV item
  `0xCB` (`StereoPairTimeOut`). This is why a pair dissolves on its own when the
  partner is switched off.
- **`DDMSMaster::handleClient only one client allowed in stereo pair`** — a
  stereo pair is hard-limited to exactly one partner, which matches the single
  entry the integration sees in the multi-room `paired[]` array.
- Issuing `MASTERLEFT`/`MASTERRIGHT` internally does a `SETFREE` first, then
  re-arms — so re-pairing does not require an explicit unpair.

Two incidental details worth recording: `SendMBResponse(…, 0x6C, 2, …)` is used
for failures, so **status byte `2` in a 7777 reply header means error**; and
`setupStereoPair` special-cases current-source ids `25` (Analog IN, already
known) and **`14`** — an otherwise unidentified source id.

### `0xD0` / `NV Read` — a named property store (read *and* write)

Op `0xD0` is far more than the single `READ_fwdownload_xml` we knew about. The
handler (`IncomingHouseKeeping` case `0xD0`, decompiled) supports **two** verbs
against `LibreEnv`, the on-device property store:

- `READ_<name>` → replies `<name>:<value>` (via `GetEnvItemByName`).
- `WRITE_<name>,<value>` → sets the item (via `SetEnvItemByName`), then replies
  with the read-back `<name>:<value>`. The payload is split on the **first
  comma**. On failure either verb replies with **status byte `2`** (error).

The property namespace is **255 named items** (the full `LibreEnv` name table).
That turns `0xD0` into a broad, low-effort monitoring surface over the channel
the integration already speaks. The monitoring-relevant items:

| Group | NV items (use `READ_<name>`) |
|---|---|
| Firmware / versions | `Firmware_version`, `FwVersion`, `MCUVersion`, `CUSTVersion`, `SPKFWVersion`, `GCASTVersion`, `cast_version`, `Hardware_version`, `HardwarePlatform` |
| Identity | `Serial_num`, `Model`, `Model_num`, `Manufacturer`, `Brand`, `ProductName`, `ProductType`, `SingleSpeaker`, `FriendlyName`, `uuid` |
| Playback state | `current_volume`, `current_mute`, `PlayerState`, `LastPlayedURL`, `Scene_Name`, `RebootSource` |
| Multi-room / DDMS | `StereoPairMode`, `StereoPairTimeOut`, `ddms_sp_type`, `ddms_channel`, `ddms_SSID`, `ddms_BAND`, `ddms_rate`, `zoneid`, `mramode`, `hostpresent` |
| Network | `ssid`, `netif`, `activeinterface`, `wifiband`, `IPADDR`, `staticip`, `netmask`, `gateway`, `primdns`, `secdns`, `Country` |
| Feature flags | `telnet`, `GoogleCast`, `SpotifyEnabled`, `airplay`, `WACMode` |
| LEDs | `LEDControl`, `LED_INTENSITY`, `LED_RGB`, `LED_FLASHING`, `LED_AMBER`, `LED_WHITE`, `LED_DEVICE` |
| Presets / favourites | `GEN_FAV_0`…`GEN_FAV_9`, `SPT_Preset1`…`SPT_Preset3` |
| Bluetooth | `BT_DeviceName`, `REMOTE_BD_ADDR` |
| Update | `fwdownload_xml`, `fwupdate_link`, `otaupdate_link`, `ScheduledUpdateTime`, `fwupdate_success` |

The CLI carries the full 255-name table:

```bash
revox_cli.py <ip> nv FwVersion          # read one item
revox_cli.py <ip> nvdump                # curated monitoring set
revox_cli.py <ip> nvdump live           # items seen populated on a real A100
revox_cli.py <ip> nvdump all --set      # probe all 255, hide empties
revox_cli.py <ip> nvwrite LED_INTENSITY 5
revox_cli.py - nvlist ddms              # offline name/UID search
```

Values that are secrets or personally identifying (Wi-Fi PSK, tokens, UUIDs,
URLs, GPS) are **masked by default** so `nvdump` output is safe to paste into an
issue; `--show-secrets` opts back in. Writing network- or boot-critical items
(`ssid`, `passphrase`, `netif`, `staticip`, `factory_reset`, …) requires
`--force`. Some items only take effect after a service or speaker restart, so a
successful read-back is not proof the change is live.

**Where the values live, and how the name table was verified.** The store is the
`fenv` / `cenv` / `senv` MTD partitions — note these do *not* appear in the
firmware's own `mtdparts`; a real A100 has 18 partitions, not 14. Each is a
sequence of **2048-byte slots**:

```
offset 0:      "SENV" magic
each 0x800:    [uid u8][flag u8][value bytes … NUL]
               flag 0xFE = live, 0xFC = superseded, 0xFF = erased
```

**`uid` is a 1-based index into the `LibreEnv` name table**, which is what makes
the ordering load-bearing. This was confirmed by dumping the partitions off a
real A100 and matching 8/8 anchors against the `SetEnvItemByID()` calls
decompiled from `luci_service` (`0x02` `ssid`, `0x0C` `FwVersion`, `0x12` `uuid`,
`0x1F` `zoneid`, `0x20` `ddms_sp_type`, `0xCA` `StereoPairMode`, …). The device
also confirmed the integration's own parsing: `FwVersion` = `p3957` and
`MCUVersion` = `44`, matching the `LS9 V3957 / Controller V44` it displays.
`tests/test_nv.py` pins those anchors so a future edit can't silently shift the
table. Superseded (`0xFC`) records keep **previous** values, which is a useful
audit trail — they are how we know a `GoogleCast=true` write had been made and
then reverted by the device.

Two related debug hooks on the same channel: **op `0xFA` (`Log Dump`) with
payload `"1"` runs `system("start adbd")`** — a network ADB enable, mirroring
the `select_telnet_adb.asp` web page — while `"0"` uploads logs to a hard-coded
Libre FTP server and `"USBLOGS"` dumps `logcat`/`dmesg` to a USB stick.

## The speaker is two computers

This is the single most useful thing to know before hunting for more commands.

| | Libre **LS9** board (Linux) | **ATMEL** host MCU |
|---|---|---|
| Runs | Wi-Fi, streaming, AirPlay/Spotify/Cast, DDMS | Revox-specific hardware |
| Owns | **port 7777 (LUCI)**, the NV store | **port 50007 (group 2/3)** |
| Features | multi-room over Wi-Fi, NV items, LED, standby | **Kleernet radio**, battery, loudness, aux, volume knob, power |
| Firmware | `LS9 V3957` (what we dumped) | `Controler V44` (device status `mcuType: 1`) |

The two are joined by a **UART** (`/dev/ttyS0`, NV `HOST_BAUDRATE`, `UART_Mode`,
`hostpresent`, `xmodem_pkt_size`; LUCI messages `Host version info`,
`HostImage_Ready`, `host App control`, `UART_READY`). The Linux side proxies
port 50007 through to the MCU — LUCI is, by design, Libre's *host controller*
interface.

The evidence is decisive: `Kleernet`, `timersty`, `PowerOnSrc`, `DisAutoAux`,
`basssboost`, `maxvolume` and `timerstandby` appear in **zero** bytes of the
Linux rootfs *and* zero bytes of the 125 MB `app` partition (which turned out to
be `/system/chrome`, the Cast receiver — see the mount in
`sbin/mount_partition.sh`). They are not there because they never were: that
protocol is implemented on the other chip. This is also why the firmware update
URL path is `A100**ATMEL**`.

**Consequence:** no further analysis of the Linux firmware can expand the group
2/3 command map, Kleernet included. The remaining routes are (a) empirical
probing of a live speaker, or (b) obtaining the ATMEL image — which the dead
`update.revox.de` endpoint was meant to serve.

> Provenance & scope: everything in the LUCI sections above is decompiled from
> the shared LS9/LibreSync network image and applies to port 7777 only.

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
| `binary_sensor` Partner speaker paired | `paired[]` from group 3 / `0x03` (**not** event `0x67`) | **Packet-capture verified** |
| `button` Pair speaker | group 3 / `0x01` (enter Kleernet pairing mode) | **Verified on a live speaker** |
| `button` Unpair speaker | group 3 / `0x05` + partner serial (read from `paired[]` at press time — never configured) | **Verified on a live speaker** |
| `revox_studioart.unpair_speaker` service | same, with an optional explicit `serial` | **Verified on a live speaker** |
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
is not reported while charging), `255` = fully charged / on mains. The same
encoding is used for the partner in `paired[]`. Battery is **poll-only**: no
push was seen in either charging capture (the LUCI `BatteryPower` op `0xE8`
never appears on the wire), and after unplugging, the chief went straight from
`254` to a real SoC (`49`, then `48`).

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

## Resolved from firmware (verify on hardware before relying on them)

Decompiling `luci_service` (see *The port-7777 protocol is Libre "LUCI"*)
identified the `Op` bytes for several previously-open items. These are
firmware-derived, not yet replayed on a live speaker:

- **Pair/unpair flow** — two separate mechanisms, don't conflate them:
  - **Kleernet (ATMEL, port 50007):** **`group 3 / 0x05` + the partner's
    serial in ASCII UNPAIRS** it (packet-capture verified, ~4 s to take
    effect). Pairing is *not* a network command — the app only sends
    `group 3 / 0x01` (probably "enter pairing mode") and the bind then happens
    over the Kleernet radio.
  - **DDMS (Libre, port 7777):** `SETFREE` on op `0x64` (`ddms`) leaves a
    Wi-Fi group, alongside `SETMASTER`/`SETSLAVE`/`JOIN*`/`DROP*`; the
    `SETSTEREO`/`SETLEFT`/`SETRIGHT` the integration already sends stay on
    `0x6A`. Payload lengths are checked exactly and an incomplete transition
    auto-reverts to free after 35 s. `PairMode`/`PairStatus`/`ShareMode` are
    `0xD7`/`0xDD`/`0xD6`. This is *firmware-derived, not yet wire-verified*.
- **Firmware-update trigger** — `SPEAKER_FW_UPDATE` = `0xEC` (the app's "update
  now"); the upgrade handshake is `FwUpgrade` `0x41` → `Firmeware_progress`
  `0x42`, and the XML URL is read via `READ_fwdownload_xml` on `0xD0`.
- **Reboot** — `RebootRequest` = `0x72` (a LUCI alternative to the binary
  `group 2 0x4D` = `2` path).
- **Network standby** — `NET_STANDBY_START`/`END` = `0x16`/`0x17`,
  `STANDBY_STATUS` = `0x18` (distinct from the group-2 *standby timer*, which is
  still open below).
- **Factory reset** — `FACTORY_DEFAULT` = `0x96` (destructive).

## Still unmapped — contributions welcome

These live in the **port-50007 binary triplet protocol**, which runs on the
**ATMEL host MCU** (see *The speaker is two computers*), so no amount of Linux
firmware analysis will resolve them — they need a live capture or a probe:

### What unpair / re-pair actually looks like on the wire

From a full packet capture of the StudioART app un-pairing and re-pairing a
partner speaker (Büro2, serial `SAAD11958`):

| t (s) | Direction | Event |
|---|---|---|
| 0.0 | speaker | `paired=[{name: Büro2, channel: 1}]` |
| **5.6** | **app → speaker** | **`group 3 / 0x05` payload `SAAD11958`** |
| **9.7** | speaker | **`paired=[]` — unpaired** |
| 12.2 | app → speaker | `group 3 / 0x01`, empty payload, no reply |
| **23.3** | speaker | `paired=[{Büro2, channel: 0}]` — re-pairing |
| 23.6 | speaker | `paired=[{Büro2, channel: 1}]` — paired |

What this establishes:

- **`0x05` + serial is UNPAIR.** An earlier mirror-only capture made it look
  like "pair" because the payload is the partner's serial; correlating with
  `paired[]` before and after shows it *removes* that partner. The effect is
  **not immediate** — about four seconds — so poll rather than assuming.
- **Pairing does not happen over the network.** Between the unpair and the
  partner reappearing, the app sent exactly one control command (`0x01`) and
  three `READ_fwdownload_xml` queries on port 7777. Nothing else. The bind
  itself runs over the **Kleernet radio** between the two speakers, so `0x01`
  is most likely "enter pairing mode" — probable, not proven, since a button on
  the speaker could equally have started it.
- **`channel` passes through `0` while binding** and settles at `1`, so a
  transient `channel: 0` means "pairing in progress".
- **`0x67` stayed `FREE,STEREO` for the entire cycle** — paired, unpaired and
  re-paired. Third independent confirmation that `0x67` reports **DDMS**
  (Wi-Fi multi-room) state and says nothing about Kleernet pairing. Use
  `paired[]` from group 3 / `0x03` for that.

**Group 3 has now been swept end to end (`0x00`–`0xFF`) and is complete.** It is
a sparse namespace — everything above `0x58` is silent. The full map:

| Cmds | Meaning |
|---|---|
| `0x03` / `0x04` / `0x05` | multi-room state, **unpair by serial** |
| `0x06` / `0x07` / `0x08` | unidentified — read `6` |
| `0x09` / `0x0A` / `0x0B` | unidentified — read `0` |
| `0x0C` / `0x0D` / `0x0E` | unidentified — read `0` |
| `0x0F` | Check P100 (action, no reply) |
| `0x10` / `0x11` / `0x12` | unidentified — read `1` |
| `0x56` / `0x57` / `0x58` | Kleernet config (`D83Fre`, `DisAutoAux`) |

`0x01` is also sent by the StudioART app (seen on the mirror) but never answers,
so it is probably an action rather than a get.

So there is no hidden Kleernet RSSI/link-quality register to find — if that data
is exposed at all, it is inside the `paired[]` array or on the ATMEL MCU only.
**The remaining work is semantic, not discovery:** identify what the four
`{"value":n,"ID":""}` triplets mean. `revox_cli.py <ip> kleernet` prints all of
them, so the method is to diff that output between states. One such experiment
is already done: **they do not change across a full unpair/re-pair cycle**, so
they are not pairing or partner-presence indicators. Still untried: changing the
Kleernet band, swapping L/R, and powering the partner off. Things that
plausibly exist but have never been looked for: Kleernet link quality/RSSI,
radio TX power, the pair/unpair flow on the *Kleernet* side (distinct from the
DDMS `SETFREE` on 7777), channel/slot assignment, and per-partner link stats.
Two ways to hunt:

```bash
revox_cli.py <ip> kleernet                       # everything known, in one report
revox_cli.py <ip> scan 3 0x00 0x40 --i-understand  # sweep for unknown replies
```

`scan` sends empty-payload gets and reports anything that answers. It is still a
probe of an undocumented MCU — prefer `watch` plus the StudioART app first,
which is risk-free.

> **An empty-payload SET writes `0`.** The first version of `scan` skipped only
> *known* sets, and a real run zeroed two undocumented group-3 settings
> (get `0x06` went `6`→`0`, get `0x10` went `1`→`0`). `scan` now **infers** sets
> from the triplet layout — the moment a probe of `N` answers with `N+1`, it
> marks `N+2` as a set and skips it — and aborts if a reply comes back numbered
> `N-1`, which means `N` was itself a set. `tests/test_scan.py` replays that
> exact device layout and asserts zero writes.

Also learned the hard way: **the speaker fans every reply out to all open
port-50007 connections.** A scan therefore sees Home Assistant's and the app's
poll replies interleaved with its own; only frames numbered `N` or `N+1` belong
to the probe. The same is true for any client — don't assume a frame you read
was a response to something you asked.

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

Findings are best reported as an
[issue](https://github.com/JohnnyLeone/hass-studioart/issues) with the
`watch` output attached.
