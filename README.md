# Revox STUDIOART for Home Assistant

Control your **Revox STUDIOART A100 / S100** speakers from Home Assistant —
entirely on your local network, no cloud account needed.

Changes made in the StudioART app (or on the speaker itself) show up in Home
Assistant within a second, and everything you change in Home Assistant is
picked up by the app just as fast.

> **Already using AirPlay or Spotify Connect?** Keep using them — the speaker
> supports both natively. This integration adds what AirPlay can't do: standby,
> sound settings, multi-room channel assignment, switching to presets, Aux or
> Bluetooth, and battery / Wi-Fi status.

## Features

**Media player**

- Volume, mute, play/pause
- Source selection: Presets 1–5, Bluetooth, Analog IN (AirPlay and Spotify
  show up automatically when a device connects)
- Now-playing info: track, artist, album, cover art and progress bar
- Play radio streams and URLs, browse Home Assistant's media library
- Text-to-speech announcements (see below)

**Sound & behaviour controls**

- Loudness, Bass boost, Max volume limit
- Aux-In trigger (auto-switch to Aux when a signal is present) and its high
  sensitivity option
- Auto power on, Power-on source, Switch L/R channel

**Multi-room**

- Stereo / Left / Right channel assignment (as in the app's "Multi-room
  Speaker Setting")
- Kleernet wireless band selection
- Paired speaker info: name, volume, channel and battery of the partner speaker
- *Partner speaker paired* sensor, plus **Pair speaker** and **Unpair speaker**
  buttons — the partner's serial is read from the speaker, so there is nothing
  to look up or type
- "Check P100" button for a wired P100 partner speaker

**Diagnostics**

- Battery level and charging status (for the speaker and its paired partner)
- Wi-Fi network and signal quality, IP address, firmware version

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JohnnyLeone&repository=hass-studioart&category=integration)

1. Click the badge above (or: HACS → three-dot menu → **Custom repositories** →
   add `https://github.com/JohnnyLeone/hass-studioart` with type **Integration**).
2. Download **Revox STUDIOART**.
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/revox_studioart/` into your Home Assistant
   `config/custom_components/` directory (so that
   `config/custom_components/revox_studioart/manifest.json` exists).
2. Restart Home Assistant.

## Setup

Your speaker is usually **discovered automatically** — accept the notification
under **Settings → Devices & Services**.

To add it manually, click the badge:

[![Open your Home Assistant instance and start setting up the Revox STUDIOART integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=revox_studioart)

(or: **Settings → Devices & Services → Add Integration → "Revox STUDIOART"**)
and enter the speaker's IP address — you can find it in the StudioART app
under speaker settings. The integration identifies the speaker by its serial
number, so a changing DHCP address is handled automatically.

## Announcements & text-to-speech

The media player works directly with Home Assistant's TTS and media browser:

```yaml
service: tts.speak
target:
  entity_id: tts.google_translate
data:
  media_player_entity_id: media_player.studioart_a100
  message: "Dinner is ready!"
```

For this to work the speaker must be able to reach your Home Assistant URL
(**Settings → System → Network**). Playing an announcement interrupts the
current source like any other stream — there is no automatic pause/resume.
(Music Assistant users can alternatively route announcements through its
AirPlay provider, which does handle resume.)

## Good to know

- **No power button.** The speaker reports no usable power state and has no
  network wake command, so the media player deliberately has no on/off.
  Standby can still be triggered with the `revox_studioart.send_command`
  service (`ascii: timerstandby`).
- **Battery while charging.** The speaker doesn't report a percentage while
  charging. The battery sensor then holds the last known level (marked with a
  charging icon and a `soc_is_last_known` attribute), and the separate
  *Battery charging* binary sensor carries the app's "Charging" status.
- **Bass boost and Max volume limit** aren't reported back by the speaker, so
  Home Assistant shows the last value it sent (kept across restarts).
- **Paired speaker settings.** A Kleernet-paired client speaker cannot be
  configured over the network while paired. To change its own settings: unpair
  (the **Unpair speaker** button, or the `revox_studioart.unpair_speaker`
  service), configure it directly, then pair again. Volume and channel stay
  managed through the main speaker.
- **Pairing happens over the radio.** *Unpair speaker* is a real network
  command and takes a few seconds to show up. *Pair speaker* only puts the
  speaker into pairing mode — the two speakers then find each other over
  Kleernet, so the partner may need putting into pairing mode as well, and
  it can take ten seconds or so before it reappears.
- **"Paired" means Kleernet, not Wi-Fi.** The *Partner speaker paired* sensor
  reflects the actual Kleernet bind. The separate `ddms_state` attribute is
  the Wi-Fi multi-room state and stays `FREE` even with a partner bound.
- **Restart button** makes the speaker drop off the network for a short
  while; the integration shows it as unavailable until it reconnects.

## Troubleshooting

- **Speaker shows as unavailable.** Check that the speaker is reachable from
  the Home Assistant host (`ping <speaker-ip>`); the integration talks to TCP
  ports 50007 and 7777 directly, so VLAN/firewall rules between HA and the
  speaker must allow those.
- **Toggles in the StudioART app flicker briefly** while Home Assistant is
  polling. This is cosmetic (the app misrenders mirrored status reads — it
  does the same to other clients) and the integration polls the affected
  settings only once a minute to keep it rare.
- **Download diagnostics** from the device page (Settings → Devices &
  Services → the speaker → three-dot menu) when reporting an
  [issue](https://github.com/JohnnyLeone/hass-studioart/issues).

## For developers

The integration is built on a reverse-engineered protocol; the full wire
documentation — frame formats, command tables, event opcodes, and the list of
still-unmapped commands — lives in **[docs/PROTOCOL.md](docs/PROTOCOL.md)**.

[`tools/revox_cli.py`](tools/revox_cli.py) is a dependency-free CLI that
speaks the same protocol, useful for testing a speaker without Home Assistant
and for mapping unknown commands:

```bash
python3 tools/revox_cli.py 192.168.42.163 status
```

The `revox_studioart.send_command` service exposes the raw ASCII and binary
command channels for experiments from within Home Assistant.

## Disclaimer

This project is not affiliated with, endorsed by, or supported by Revox GmbH.
The protocol was reverse-engineered from local network traffic of the owner's
own speakers. Use at your own risk. "Revox" and "STUDIOART" are trademarks of
their respective owner.

## License

[MIT](LICENSE)
