from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client, config_validation as cv

from .api import EmporiaAuthError, EmporiaClient, EmporiaError
from .const import DEFAULT_SCALES, DEFAULT_WS_URL, DOMAIN, USAGE_SCALES

_LOGGER = logging.getLogger(__name__)


class EmporiaVueConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Emporia Vue WS add-on."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            ws_url = user_input["ws_url"]
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(f"{DOMAIN}_{ws_url}")
            self._abort_if_unique_id_configured()

            session = aiohttp_client.async_get_clientsession(self.hass)
            client = EmporiaClient(
                session=session,
                ws_url=ws_url,
                username=username,
                password=password,
            )
            try:
                await client.async_authenticate()
                await client.async_get_devices()
            except EmporiaAuthError:
                errors["base"] = "invalid_auth"
            except EmporiaError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Emporia Vue ({ws_url})",
                    data={
                        "ws_url": ws_url,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )
            finally:
                await client.async_close()

        data_schema = vol.Schema(
            {
                vol.Required("ws_url", default=DEFAULT_WS_URL): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )


class EmporiaVueOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Emporia Vue options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title="Emporia Vue options", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(
                    "scales",
                    default=self.config_entry.options.get("scales", DEFAULT_SCALES),
                ): vol.All(cv.ensure_list, [vol.In(USAGE_SCALES)]),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=data_schema, errors=errors
        )


@config_entries.HANDLERS.register(DOMAIN)
class EmporiaVueFlowHandler(EmporiaVueConfigFlow):
    """Shim to allow options flow registration in HA <2024.8 if needed."""

    pass


async def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> EmporiaVueOptionsFlowHandler:  # type: ignore
    return EmporiaVueOptionsFlowHandler(config_entry)
