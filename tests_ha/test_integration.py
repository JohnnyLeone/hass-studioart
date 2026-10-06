"""Integration tests: setup, entities, services and the config flow."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.revox_studioart.api import RevoxError
from custom_components.revox_studioart.const import DOMAIN

from .conftest import HOST, SERIAL, make_state

# Every unique id the integration has shipped. Changing one orphans the
# user's entity (and its history), so this list must only ever grow.
EXPECTED_UNIQUE_IDS = {
    f"{SERIAL}_{key}"
    for key in (
        "media_player",
        "aux_trigger",
        "aux_high_sens",
        "loudness",
        "lr_swap",
        "autopoweron",
        "bassboost",
        "channel",
        "poweronsrc",
        "kleernet_band",
        "wifi_ssid",
        "wifi_rssi",
        "ip",
        "brightness",
        "battery",
        "paired_speaker",
        "paired_battery",
        "maxvolume",
        "battery_charging",
        "paired_battery_charging",
        "kleernet_paired",
        "restart",
        "identify_paired",
        "kleernet_pair_mode",
        "kleernet_unpair",
    )
}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{SERIAL}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


async def test_setup_creates_all_entities_with_stable_ids(hass, client, entry):
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    client.start_events.assert_called_once()

    registry = er.async_get(hass)
    unique_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert unique_ids == EXPECTED_UNIQUE_IDS

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    client.stop_events.assert_awaited()


async def test_setup_retries_when_speaker_unreachable(hass, client, entry):
    client.async_get_state.side_effect = RevoxError("down")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_entity_states(hass, client, entry):
    await _setup(hass, entry)
    player = hass.states.get(_entity_id(hass, "media_player", "media_player"))
    assert player.state == "idle"
    assert player.attributes["source"] == "Bluetooth"
    assert player.attributes["volume_level"] == 0.3
    assert hass.states.get(_entity_id(hass, "select", "poweronsrc")).state == (
        "Bluetooth"
    )
    assert hass.states.get(_entity_id(hass, "sensor", "battery")).state == "64"
    assert hass.states.get(_entity_id(hass, "sensor", "paired_battery")).state == "80"
    assert hass.states.get(_entity_id(hass, "sensor", "paired_speaker")).state == (
        "Buero2"
    )
    unpair = hass.states.get(_entity_id(hass, "button", "kleernet_unpair"))
    assert unpair.attributes["target_serial"] == "SAAD11958"


async def test_battery_holds_last_soc_while_charging(hass, client, entry):
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    battery = _entity_id(hass, "sensor", "battery")

    coordinator.async_set_updated_data(make_state(battery=None, battery_charging=True))
    await hass.async_block_till_done()
    state = hass.states.get(battery)
    assert state.state == "64"
    assert state.attributes["charging"] is True
    assert state.attributes["soc_is_last_known"] is True


async def test_partner_entities_unavailable_without_partner(hass, client, entry):
    client.async_get_state.return_value = make_state(paired=[])
    await _setup(hass, entry)
    for platform, key in (
        ("sensor", "paired_speaker"),
        ("sensor", "paired_battery"),
        ("binary_sensor", "paired_battery_charging"),
        ("button", "kleernet_unpair"),
    ):
        state = hass.states.get(_entity_id(hass, platform, key))
        assert state.state == STATE_UNAVAILABLE, key
    paired = hass.states.get(_entity_id(hass, "binary_sensor", "kleernet_paired"))
    assert paired.state == "off"


async def test_commands_reach_the_client(hass, client, entry):
    await _setup(hass, entry)
    player = _entity_id(hass, "media_player", "media_player")

    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": player, "volume_level": 0.42},
        blocking=True,
    )
    client.set_volume.assert_awaited_once_with(42)

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": player, "source": "Analog IN"},
        blocking=True,
    )
    client.select_source_id.assert_awaited_once_with(25)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": _entity_id(hass, "select", "kleernet_band"), "option": "5.8 GHz"},
        blocking=True,
    )
    client.set_kleernet_band.assert_awaited_once_with(3)

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity_id(hass, "switch", "loudness")},
        blocking=True,
    )
    client.set_loudness.assert_awaited_once_with(True)


async def test_unknown_source_is_rejected(hass, client, entry):
    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {
                "entity_id": _entity_id(hass, "media_player", "media_player"),
                "source": "Tape",
            },
            blocking=True,
        )


async def test_command_failure_is_a_readable_error(hass, client, entry):
    await _setup(hass, entry)
    client.set_bass_boost.side_effect = RevoxError("connection refused")
    switch = _entity_id(hass, "switch", "bassboost")
    with pytest.raises(HomeAssistantError, match="connection refused"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch}, blocking=True
        )
    # a failed command must not flip the optimistic state
    assert hass.states.get(switch).state == "off"


async def test_play_pause_is_optimistic(hass, client, entry):
    await _setup(hass, entry)
    player = _entity_id(hass, "media_player", "media_player")
    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": player}, blocking=True
    )
    client.play.assert_awaited_once()
    assert hass.states.get(player).state == "playing"

    # once confirmed, a change made elsewhere shows immediately
    coordinator = entry.runtime_data
    coordinator.async_set_updated_data(make_state(play_state=1))
    coordinator.async_set_updated_data(make_state(play_state=2))
    await hass.async_block_till_done()
    assert hass.states.get(player).state == "paused"


# -- services ----------------------------------------------------------------


async def test_unpair_service_uses_the_bound_partner(hass, client, entry):
    await _setup(hass, entry)
    await hass.services.async_call(
        DOMAIN, "unpair_speaker", {"entry_id": entry.entry_id}, blocking=True
    )
    client.kleernet_unpair.assert_awaited_once_with("SAAD11958")


async def test_unpair_service_rejects_unknown_serial(hass, client, entry):
    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "unpair_speaker",
            {"entry_id": entry.entry_id, "serial": "SNOPE0000"},
            blocking=True,
        )
    client.kleernet_unpair.assert_not_awaited()


async def test_service_never_guesses_between_speakers(hass, client, entry):
    await _setup(hass, entry)
    other = MockConfigEntry(
        domain=DOMAIN, unique_id="SOTHER", data={CONF_HOST: "192.0.2.11"}
    )
    await _setup(hass, other)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "unpair_speaker", {"entry_id": "bogus"}, blocking=True
        )
    client.kleernet_unpair.assert_not_awaited()


async def test_send_command_validation(hass, client, entry):
    await _setup(hass, entry)
    # nothing to send
    with pytest.raises(Exception, match="at least one"):
        await hass.services.async_call(
            DOMAIN, "send_command", {"entry_id": entry.entry_id}, blocking=True
        )
    # incomplete binary triple
    with pytest.raises(Exception, match="must be given together"):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"entry_id": entry.entry_id, "bin_group": 2},
            blocking=True,
        )
    await hass.services.async_call(
        DOMAIN,
        "send_command",
        {"entry_id": entry.entry_id, "bin_group": 2, "bin_cmd": 91, "bin_value": 1},
        blocking=True,
    )
    client.async_set_bin.assert_awaited_once_with(2, 91, 1)


# -- config flow -------------------------------------------------------------


async def test_user_flow(hass, client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 50007}


async def test_user_flow_cannot_connect(hass, client):
    client.async_get_state.side_effect = RevoxError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_updates_host(hass, client, entry):
    await _setup(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.99"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.0.2.99"


async def test_reconfigure_refuses_a_different_speaker(hass, client, entry):
    await _setup(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    client.async_get_state.return_value = make_state(serial="SOTHER")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.99"}
    )
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_HOST] == HOST
