"""Constants for the Revox STUDIOART integration."""

from __future__ import annotations

DOMAIN = "revox_studioart"

# The STUDIOART control port. It speaks two protocols on the same TCP port:
#   * a binary, length-prefixed request/response protocol (used for status reads)
#   * an ASCII "cmd ...\r\n" telnet-style protocol (used for control)
DEFAULT_PORT = 50007

# Event/push channel. The speaker mirrors every command any client sends to
# subscribers on this port, and it is also the channel the official app uses
# for SETSTEREO/SETLEFT/SETRIGHT and READ_* queries.
EVENT_PORT = 7777

DEFAULT_NAME = "STUDIOART Speaker"
MANUFACTURER = "Revox"

DEFAULT_SCAN_INTERVAL = 10  # seconds
CONNECT_TIMEOUT = 4.0
SOCKET_TIMEOUT = 4.0

# Config entry keys
CONF_HOST = "host"
CONF_PORT = "port"

# ---------------------------------------------------------------------------
# Sources. Two mechanisms exist:
#   * numeric ids via binary set group 2 / 0x03 (what the app's Source tab
#     sends) — device-verified: 19 = Bluetooth, 25 = Analog IN
#   * documented ASCII commands for the presets
# The playback status reports the numeric id ("source"). Id 1 is an active
# AirPlay session: AirPlay cannot be *selected* — it activates itself when a
# client connects — so it is display-only.
# ---------------------------------------------------------------------------
SOURCE_IDS: dict[str, int] = {
    "Bluetooth": 19,
    "Analog IN": 25,
}
SOURCE_ID_TO_NAME: dict[int, str] = {v: k for k, v in SOURCE_IDS.items()}
SOURCE_ID_TO_NAME[1] = "AirPlay"

SOURCE_COMMANDS: dict[str, str] = {
    "Preset 1": "source preset 0",
    "Preset 2": "source preset 1",
    "Preset 3": "source preset 2",
    "Preset 4": "source preset 3",
    "Preset 5": "source preset 4",
}

# Multi-room channel assignment ("Multi-room Speaker Setting" in the app).
# Sent over the event channel (op 0x6A) as the official app does; the speaker
# answers with an 0x67 status push of the form "FREE,STEREO,<concurrent-SSID>".
# Option labels follow the app's radio buttons.
CHANNEL_COMMANDS: dict[str, str] = {
    "Stereo": "SETSTEREO",
    "Left channel": "SETLEFT",
    "Right channel": "SETRIGHT",
}
CHANNEL_OPTIONS = list(CHANNEL_COMMANDS)
# maps the token in the 0x67 push back to the option name
CHANNEL_TOKEN_TO_OPTION = {
    "STEREO": "Stereo",
    "LEFT": "Left channel",
    "RIGHT": "Right channel",
}

# Power-on source (device status field "PowerOnSrc", set = group 2 / 0x58).
# All indices confirmed on the wire by cycling the app's menu.
POWER_ON_SOURCE_OPTIONS: dict[int, str] = {
    0: "Last played",
    1: "Preset 1",
    2: "Preset 2",
    3: "Preset 3",
    4: "Preset 4",
    5: "Preset 5",
    6: "Bluetooth",
    7: "Analog IN",
}

# Kleernet wireless band ("D83Fre" in the Kleernet JSON, set = group 2 / 0x9B).
KLEERNET_BAND_OPTIONS: dict[int, str] = {
    0: "Automatic",
    1: "2.4 GHz",
    2: "5.2 GHz",
    3: "5.8 GHz",
}
