from copy import deepcopy
from typing import Any, Dict, Optional
from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
import logging
from homeassistant.helpers.entity_registry import (
    async_entries_for_config_entry,
    async_get_registry,
)
from custom_components.rte_ecowatt.sensor import SENSOR_SCHEMA
from .const import (
    CONF_CLIENT_SECRET,
    CONF_CLIENT_ID,
    CONF_SENSOR_UNIT,
    CONF_SENSOR_UNIT_OPTIONS,
    CONF_SENSOR_SHIFT,
    CONF_SENSORS,
    CONF_SENSOR_NEW,
    DOMAIN,
)


_LOGGER: logging.Logger = logging.getLogger(__package__)


SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SENSOR_UNIT): vol.In(CONF_SENSOR_UNIT_OPTIONS),
        vol.Required(CONF_SENSOR_SHIFT): cv.positive_int,
        vol.Optional(CONF_SENSOR_NEW): cv.boolean,
    }
)

class AbstractRTEEcowattOptionsFlowHander(config_entries.OptionsFlow):

    data: Optional[Dict[str, Any]]

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_sensor(
        self, user_input: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        _LOGGER.debug("in option sensor config form FOR UPDATE...")
        if user_input is not None:
            _LOGGER.debug(f"in Abstract during update before append {self.data}")
            self.data[CONF_SENSORS].append(
                {
                    "unit": user_input[CONF_SENSOR_UNIT],
                    "shift": user_input[CONF_SENSOR_SHIFT],
                }
            )
            _LOGGER.debug(f"in Abstract during update AFTER append {self.data}")
            if user_input.get(CONF_SENSOR_NEW, False):
                return await self.async_step_sensor()
            # Removed this step since other UPDATE not transmitted
            return self.async_create_entry(title=DOMAIN, data=self.data)

        return self.async_show_form(
            step_id="sensor",
            data_schema=SENSOR_SCHEMA,
            # errors=self._errors,
        )


# Source : https://aarongodfrey.dev/home%20automation/building_a_home_assistant_custom_component_part_4/
class RteEcowattFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    # class RteEcowattFlowHandler(
    #     AbstractRTEEcowattOptionsFlowHander,
    #     config_entries.ConfigFlow,
    # ):
    """Config flow for RteEcoWatt."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL
    data: Optional[Dict[str, Any]]

    # def __init__(self, config_entry: config_entries.ConfigEntry, domain=DOMAIN):
    #     """Initialize."""
    #     super.__init__(config_entry)
    #     self._errors = {}

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        self._errors = {}

        # Uncomment the next 2 lines if only a single instance of the integration is allowed:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            _LOGGER.debug(f"user_input for init:  {user_input}")
            valid = await self._test_credentials(
                user_input[CONF_CLIENT_ID], user_input[CONF_CLIENT_SECRET]
            )

            if valid:
                self.data = user_input
                self.data[CONF_SENSORS] = []
                _LOGGER.debug("Config is valid !")
                return await self.async_step_sensors()
            else:
                self._errors["base"] = "auth"

        user_input = {}
        # Provide defaults for form
        user_input[CONF_CLIENT_ID]=""
        user_input[CONF_CLIENT_SECRET]=""

        return await self._show_id_form(user_input)

    #FIXME this should handle update
    # @staticmethod
    # @callback
    # def async_get_options_flow(config_entry):
    #     """Get the options flow for this handler."""
    #     return RTEEcowattOptionsFlowHandler(config_entry)

    async def async_step_sensors(self, user_input: Optional[Dict[str, Any]] = None):
        _LOGGER.debug("in sensor config form")
        if user_input is not None:
            self.data[CONF_SENSORS].append(
                {
                    "unit": user_input[CONF_SENSOR_UNIT],
                    "shift": user_input[CONF_SENSOR_SHIFT],
                }
            )
            if user_input.get(CONF_SENSOR_NEW, False):
                return await self.async_step_sensors()

            return self.async_create_entry(title=DOMAIN, data=self.data)

        return self.async_show_form(
            step_id="sensors",
            data_schema=SENSOR_SCHEMA,
            errors=self._errors,
        )

    async def _show_id_form(self, user_input):  # pylint: disable=unused-argument
        """Show the configuration form to edit user data."""
        _LOGGER.debug("in show config form")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CLIENT_ID, default=user_input[CONF_CLIENT_ID]
                    ): str,
                    vol.Required(
                        CONF_CLIENT_SECRET, default=user_input[CONF_CLIENT_SECRET]
                    ): str,
                }
            ),
            errors=self._errors,
        )

    # TODO Call RTE endpoint to check if response is 200 and not 401.
    async def _test_credentials(self, client_id, client_secret):
        """Return true if credentials are valid."""
        try:
            return True
        except Exception:  # py-lint(broad-except)
            return False


class RTEEcowattOptionsFlowHandler(
    AbstractRTEEcowattOptionsFlowHander, config_entries.OptionsFlow
):
    """Handles options flow for the component."""

    data: Optional[Dict[str, Any]]

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # self.config_entry = config_entry
        super().__init__(config_entry)
        self.data = {}
        self.data[CONF_SENSORS] = []

    async def async_step_init(
        self, user_input: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Manage the options for the custom component."""
        errors: Dict[str, str] = {}
        # Grab all configured sensor from the entity registry so we can populate the
        # multi-select dropdown that will allow a user to remove a sensor.
        entity_registry = await async_get_registry(self.hass)
        entries = async_entries_for_config_entry(
            entity_registry, self.config_entry.entry_id
        )
        all_sensors = {e.entity_id: e.original_name for e in entries}
        sensor_map = {e.entity_id: e for e in entries}

        if user_input is not None:
            updated_sensor = deepcopy(self.config_entry.data[CONF_SENSORS])
            _LOGGER.debug(f"Updated sensor ={updated_sensor}")
            # Remove any unchecked sensor.
            removed_entities = [
                entity_id
                for entity_id in sensor_map.keys()
                if entity_id not in user_input["sensors"]
            ]
            _LOGGER.debug(f"removed entities: {removed_entities}")
            for entity_id in removed_entities:
                # Unregister from HA
                _LOGGER.info(f"Removing sensor {entity_id}")
                entity_registry.async_remove(entity_id)
                # Remove from our configured sensor.
                entry = sensor_map[entity_id]
                entry_path = entry.unique_id
                _LOGGER.debug(f"updated_sensor when x is removed: {updated_sensor}")
                updated_sensor = [
                    e
                    for e in updated_sensor
                    if (
                        e[CONF_SENSOR_UNIT] != entry_path
                        and e[CONF_SENSOR_SHIFT] != entry_path
                    )
                ]
            if user_input.get(CONF_CLIENT_SECRET) or user_input.get(CONF_CLIENT_ID):
                try:
                    # TODO implement method to validate clientID /client SECRET and raise errors if any
                    if not errors:
                        # Update Client id and secret
                        _LOGGER.debug("Update conf data")
                        _LOGGER.debug(f"user_input for update:  {user_input}")
                except Exception:
                    pass
                    # TODO Handle exception

            if user_input.get(CONF_SENSOR_NEW, False):
                _LOGGER.debug("Want to add New")
                # return await self.async_step_sensors()
                # Value of data will be set on the options property of our config_entry
                # instance.
                return await self.async_step_sensor()

            self.data = user_input
            _LOGGER.debug(f"self.data for update:  {self.data}")
            return self.async_create_entry(title=DOMAIN, data=self.data)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CLIENT_ID, default=self.config_entry.data[CONF_CLIENT_ID]
                ): cv.string,
                vol.Optional(
                    CONF_CLIENT_SECRET,
                    default=self.config_entry.data[CONF_CLIENT_SECRET],
                ): cv.string,
                vol.Optional(
                    "sensors", default=list(all_sensors.keys())
                ): cv.multi_select(all_sensors),
                vol.Optional(CONF_SENSOR_NEW): cv.boolean,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=options_schema, errors=errors
        )