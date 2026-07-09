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
# Source list. The A100 exposes 5 presets plus Bluetooth and Aux.
# The ASCII command for each is documented in the README.
# ---------------------------------------------------------------------------
SOURCE_COMMANDS: dict[str, str] = {
    "Preset 1": "source preset 0",
    "Preset 2": "source preset 1",
    "Preset 3": "source preset 2",
    "Preset 4": "source preset 3",
    "Preset 5": "source preset 4",
    "Bluetooth": "source BT",
    "Aux": "source aux",
}

# Multi-room channel assignment. Sent over the event channel (op 0x6A) as the
# official app does; the speaker answers with an 0x67 status push of the form
# "FREE,STEREO,<concurrent-SSID>".
CHANNEL_COMMANDS: dict[str, str] = {
    "Stereo": "SETSTEREO",
    "Left": "SETLEFT",
    "Right": "SETRIGHT",
}
CHANNEL_OPTIONS = list(CHANNEL_COMMANDS)
# maps the token in the 0x67 push back to the option name
CHANNEL_TOKEN_TO_OPTION = {
    "STEREO": "Stereo",
    "LEFT": "Left",
    "RIGHT": "Right",
}

# Power-on source (device status field "PowerOnSrc", set = group 2 / 0x9B).
# 0 = "Last Played" is confirmed on the wire; the remaining indices follow the
# app's source order and are provisional until verified.
POWER_ON_SOURCE_OPTIONS: dict[int, str] = {
    0: "Last played",
    1: "Preset 1",
    2: "Preset 2",
    3: "Preset 3",
    4: "Preset 4",
    5: "Preset 5",
    6: "Bluetooth",
    7: "Aux",
}
