"""Fixtures for the Home Assistant integration tests.

These need ``pytest-homeassistant-custom-component`` and run separately from
the HA-free protocol tests in ``tests/`` (its plugin replaces the event loop
policy, which breaks their plain ``asyncio.run`` calls)::

    pytest tests_ha
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.revox_studioart.api import RevoxState, RevoxStudioArtClient
from custom_components.revox_studioart.const import DOMAIN

HOST = "192.0.2.10"
SERIAL = "SDHD17496"
PARTNER = {
    "type": "A100",
    "name": "Buero2",
    "ID": "SAAD11958",
    "volume": 12,
    "channel": 1,
    "battery": 80,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the integration from custom_components/."""


def make_state(**over) -> RevoxState:
    fields = {
        "name": "Buero1",
        "ip": HOST,
        "mac": "AA:BB:CC:DD:EE:FF",
        "serial": SERIAL,
        "ssid": "mynet",
        "rssi": 2,
        "firmware_ls9": "V3957",
        "firmware_controller": "V44",
        "battery": 64,
        "battery_charging": False,
        "volume": 30,
        "source": 19,
        "play_state": 0,
        "aux_trigger": True,
        "loudness": False,
        "paired": [dict(PARTNER)],
        "kleernet_band": 0,
        "power_on_source": 6,
    }
    fields.update(over)
    return RevoxState(**fields)


@pytest.fixture
def client() -> Generator[MagicMock]:
    """A mocked speaker client, patched in for both setup and config flow."""
    mock = MagicMock(spec=RevoxStudioArtClient)
    for name in dir(RevoxStudioArtClient):
        attr = getattr(RevoxStudioArtClient, name)
        if name.startswith("_") or isinstance(attr, property):
            continue
        if callable(attr) and name not in ("start_events",):
            setattr(mock, name, AsyncMock())
    mock.host = HOST
    mock.events_connected = True
    mock.async_get_state.return_value = make_state()
    with (
        patch(
            "custom_components.revox_studioart.RevoxStudioArtClient",
            return_value=mock,
        ),
        patch(
            "custom_components.revox_studioart.config_flow.RevoxStudioArtClient",
            return_value=mock,
        ),
    ):
        yield mock


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Buero1",
        unique_id=SERIAL,
        data={CONF_HOST: HOST, CONF_PORT: 50007},
    )
