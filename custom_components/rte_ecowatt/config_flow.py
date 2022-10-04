import voluptuous as vol
import logging
from typing import Any, Dict, Optional, Tuple

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

_LOGGER = logging.getLogger(__name__)


class SetupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Called once with None as user_input, then a second time with user provided input"""
        errors = {}
        valid = True  # for now we hardcode this

        if user_input is not None:
            if valid:
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                # will call async_setup_entry defined in __init__.py file
                return self.async_create_entry(title="A random title", data=user_input)
            errors["base"] = "auth_error"

        data_schema = {
            vol.Required(CONF_CLIENT_ID): cv.string,
            vol.Required(CONF_CLIENT_SECRET): cv.string,
        }

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(data_schema), errors=errors
        )
