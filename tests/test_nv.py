"""Tests for the NV (LibreEnv property store) support in tools/revox_cli.py.

The NV name table is order-sensitive: the on-flash ENV records store a 1-based
UID that indexes into it, so an inserted or dropped name silently mis-labels
every item after it. These tests pin the anchors that were verified against a
real A100 (firmware LS9 3957 / MCU 44) and against the SetEnvItemByID() calls
decompiled out of luci_service.
"""

from __future__ import annotations

# uid -> name, confirmed on-device and/or in the decompiled firmware
ANCHORS = {
    0x02: "ssid",
    0x03: "security",
    0x04: "passphrase",
    0x0C: "FwVersion",
    0x12: "uuid",
    0x1F: "zoneid",
    0x20: "ddms_sp_type",
    0xCA: "StereoPairMode",
    0xCB: "StereoPairTimeOut",
}


def test_table_size(cli):
    assert len(cli.NV_ITEMS) == 255


def test_uid_anchors(cli):
    """UID is a 1-based index into NV_ITEMS — guards against drift."""
    for uid, name in ANCHORS.items():
        assert cli.NV_ITEMS[uid - 1] == name, f"uid {uid:#x} should be {name}"


def test_no_duplicate_names(cli):
    assert len(set(cli.NV_ITEMS)) == len(cli.NV_ITEMS)


def test_groups_only_contain_real_names(cli):
    """A typo in a group would silently no-op against the device."""
    for group in ("NV_MONITOR", "NV_LIVE", "NV_SECRET", "NV_DANGEROUS"):
        bogus = sorted(n for n in getattr(cli, group) if n not in cli.NV_ITEMS)
        assert not bogus, f"{group} has names not in NV_ITEMS: {bogus}"


def test_credentials_are_masked(cli):
    """Secrets must never be printed by default (nvdump output gets pasted)."""
    for name in ("passphrase", "ssid", "AirableAuth", "SP_BLOB", "Location"):
        assert name in cli.NV_SECRET
        masked = cli.nv_mask(name, "hunter2hunter2")
        assert "hunter2" not in masked and "redacted" in masked
    # ...but --show-secrets opts back in
    assert cli.nv_mask("passphrase", "hunter2", show_secrets=True) == "hunter2"


def test_non_secret_values_pass_through(cli):
    assert cli.nv_mask("FwVersion", "p3957") == "p3957"


def test_network_critical_items_are_write_guarded(cli):
    for name in ("ssid", "passphrase", "netif", "staticip", "factory_reset"):
        assert name in cli.NV_DANGEROUS


def test_name_validation_and_suggestions(cli):
    assert cli.nv_valid("GoogleCast")
    assert not cli.nv_valid("NotAnItem")
    # case-insensitive exact match wins
    assert cli.nv_suggest("fwversion") == ["FwVersion"]
    # substring search otherwise
    assert "ddms_SSID" in cli.nv_suggest("ddms_ss")
    assert cli.nv_suggest("zzzznope") == []


def test_scan_blocklist_covers_known_destructive_commands(cli):
    """`scan` must never transmit a known set/action with an empty payload.

    0x4D is the group-2 power action (value 2 = restart); probing it with an
    empty payload could trigger an unknown power state.
    """
    assert 0x4D in cli.SCAN_BLOCKLIST[2], "power action must stay blocklisted"
    for cmd in (0x03, 0x2A, 0x36, 0x43, 0x58, 0x5B, 0x62, 0x8F, 0x9B, 0x9E):
        assert cmd in cli.SCAN_BLOCKLIST[2], f"group 2 {cmd:#x} should be blocked"
    assert 0x0F in cli.SCAN_BLOCKLIST[3], "Check P100 is fire-and-forget"


def test_scan_blocklist_does_not_over_block(cli):
    """Known read-only gets must stay probeable or discovery is crippled."""
    for cmd in (0x34, 0x37, 0x3C, 0x41, 0x8D):
        assert cmd not in cli.SCAN_BLOCKLIST[2]
    for cmd in (0x03, 0x56):
        assert cmd not in cli.SCAN_BLOCKLIST[3]


# --- group 3 / 0x05: pair a partner speaker by serial number -----------------
# Captured on the mirror channel while pairing Büro2:
#   [mirror] group=3 cmd=0x05 value='534141443131393538'  -> b"SAAD11958"


def test_decode_renders_ascii_payloads_not_hex(cli):
    """The pairing SN used to render as an opaque hex blob."""
    assert cli._decode(bytes.fromhex("534141443131393538")) == "SAAD11958"


def test_decode_keeps_other_shapes(cli):
    assert cli._decode(b"") is None
    assert cli._decode(b"\x01") == 1
    assert cli._decode(b'{"a":1}') == {"a": 1}
    # non-printable stays hex
    assert cli._decode(b"\x00\xff\x01") == "00ff01"
