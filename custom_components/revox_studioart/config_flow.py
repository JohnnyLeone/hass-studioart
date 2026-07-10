"""Config flow for Revox STUDIOART."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import RevoxError, RevoxState, RevoxStudioArtClient
from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class RevoxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Revox STUDIOART."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._name: str | None = None

    async def _validate(self, host: str, port: int) -> RevoxState:
        """Connect and return the device state, or raise RevoxError."""
        client = RevoxStudioArtClient(host, port)
        return await client.async_get_state()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            try:
                state = await self._validate(host, port)
            except RevoxError:
                errors["base"] = "cannot_connect"
            else:
                # the device serial survives IP changes; fall back to the host
                await self.async_set_unique_id(state.serial or host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=state.name or host,
                    data={CONF_HOST: host, CONF_PORT: port},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via AirPlay/RAOP mDNS records."""
        props = discovery_info.properties
        model = (props.get("model") or props.get("am") or "").lower()
        manufacturer = (props.get("manufacturer") or "").lower()
        if not model.startswith("revox") and "revox" not in manufacturer:
            return self.async_abort(reason="not_studioart")

        host = discovery_info.host
        # legacy entries used the host as unique_id — don't offer them again
        self._async_abort_entries_match({CONF_HOST: host})

        # connect once to fetch the device serial so the unique_id survives
        # IP changes (the mDNS serialNumber is MAC-derived, the control
        # protocol reports the real serial, e.g. "SDHD17496")
        try:
            state = await self._validate(host, DEFAULT_PORT)
        except RevoxError:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(state.serial or host)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        self._host = host
        # discovery name is like "Buero1._raop._tcp.local." -> strip suffix
        self._name = (
            state.name
            or (discovery_info.name or "").split(".")[0].split("@")[-1]
            or host
        )
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._host is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._name or self._host,
                data={CONF_HOST: self._host, CONF_PORT: DEFAULT_PORT},
            )

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name or self._host},
        )
