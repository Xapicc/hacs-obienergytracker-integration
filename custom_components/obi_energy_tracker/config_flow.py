"""Config flow for Obi EnergyTracker integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ObiEnergyTrackerAPI
from .const import CONF_BRIDGE_ID, CONF_COUNTRY, CONF_DEVICE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_COUNTRY, default="DE"): str,
    }
)


class ObiEnergyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Obi EnergyTracker."""

    VERSION = 1
    MINOR_VERSION = 1

    # No async_supports_options_flow override here on purpose. Claiming support
    # without also providing async_get_options_flow puts a Configure button on
    # the integration card that cannot work: HA answers the click from the base
    # ConfigFlow, which raises UnknownHandler, and the frontend shows
    # "Invalid handler specified". There are no options to configure anyway --
    # credentials are re-entered by removing and re-adding the entry.

    async def async_step_discovery(  # pylint: disable=unused-argument
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle discovery."""
        return await self.async_step_user()

    async def async_step_user(  # pylint: disable=unused-argument
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Check for duplicate entries
            await self.async_set_unique_id(user_input[CONF_EMAIL])
            self._abort_if_unique_id_configured()

            # Test the connection
            session = async_create_clientsession(self.hass)
            api = ObiEnergyTrackerAPI(
                session,
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                country=user_input.get(CONF_COUNTRY, "DE"),
            )

            if await api.async_login():
                if info := await api.async_get_bridge_info():
                    user_input[CONF_BRIDGE_ID] = info["bridge_id"]
                    user_input[CONF_DEVICE_ID] = info["device_id"]
                    return self.async_create_entry(
                        title=user_input[CONF_EMAIL],
                        data=user_input,
                    )
                errors["base"] = "no_devices"
            else:
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
