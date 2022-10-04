import voluptuous as vol
import logging
from typing import Any, Dict, Optional, Tuple
from oauthlib.oauth2 import rfc6749

from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
)
from . import AsyncOauthClient

_LOGGER = logging.getLogger(__name__)


class SetupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Called once with None as user_input, then a second time with user provided input"""
        errors = {}
        valid = False

        if user_input is not None:
            _LOGGER.debug("Testing connectivity to RTE api")
            try:
                test_client = AsyncOauthClient(user_input)
                await test_client.client()
                valid = True
            except rfc6749.errors.InvalidClientError:
                _LOGGER.error(
                    "Unable to validate RTE api access. Credentials are likely incorrect"
                )
                errors["base"] = "auth_error"
            except Exception as e:
                _LOGGER.error(f"Unable to validate RTE api access, unknown error: {e}")
                errors["base"] = "generic_error"
            if valid:
                _LOGGER.debug("Connectivity to RTE api validated")
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                # will call async_setup_entry defined in __init__.py file
                return self.async_create_entry(title="ecowatt by RTE", data=user_input)

        data_schema = {
            vol.Required(CONF_CLIENT_ID): cv.string,
            vol.Required(CONF_CLIENT_SECRET): cv.string,
        }

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(data_schema), errors=errors
        )
