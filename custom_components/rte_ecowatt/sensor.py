import voluptuous as vol
from datetime import timedelta, date, datetime
from typing import Any, Callable, Dict, Optional
import logging
import json

from oauthlib.oauth2 import BackendApplicationClient
from async_oauthlib import OAuth2Session
import aiohttp

from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    TOKEN_URL,
    BASE_URL,
    ATTR_LEVEL_STRING
)

_LOGGER = logging.getLogger(__name__)


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
        {
            vol.Required(CONF_CLIENT_ID): cv.string,
            vol.Required(CONF_CLIENT_SECRET): cv.string,
        }
)


async def async_setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        async_add_entities: AddEntitiesCallback,
        discovery_info: Optional[DiscoveryInfoType] = None,
        ):
    """Set up the My EcoWatt by RTE component."""

    # get a token
    coordinator = EcoWattAPICoordinator(hass, config)
    async_add_entities(EcowattLevel(coordinator, i) for i in range(4))
    # force a first refresh immediately to avoid waiting for 16 minutes
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.info("We finished the setup of ecowatt platform")

class EcoWattAPICoordinator(DataUpdateCoordinator):
    """A coordinator to fetch data from the api only once"""

    def __init__(self, hass, config: ConfigType):
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="ecowatt api",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=16),
            update_method=self.update_method
        )
        self.config = config
        self.hass = hass

    async def async_oauth_client(self):
        client = BackendApplicationClient(client_id=self.config[CONF_CLIENT_ID])
        session = OAuth2Session(client=client)
        auth = aiohttp.helpers.BasicAuth(self.config[CONF_CLIENT_ID], self.config[CONF_CLIENT_SECRET])
        self.token = await session.fetch_token(token_url=TOKEN_URL, auth=auth)
        _LOGGER.info(self.token)
        return session 

    async def update_method(self):
        """Fetch data from API endpoint.

        This could be the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            _LOGGER.info("Starting collecting data")
            client = await self.async_oauth_client()
            headers = {
                'Authorization': f"{self.token['token_type']} {self.token['access_token']}"
            }
            headers = {
                'Authorization': f"{self.token['token_type']} {self.token['access_token']}"
            }
            url = f"{BASE_URL}/open_api/ecowatt/v4/signals"
            import os
            if 'ECOWATT_DEBUG' in os.environ:
                url = f"{BASE_URL}/open_api/ecowatt/v4/sandbox/signals"
            api_result = await client.get(url, headers=headers)
            await client.close()
            _LOGGER.info(f"data received, status code: {api_result.status}")
            if api_result.status != 200:
                # a code 429 is expected when doing development and not using the sandbox url
                _LOGGER.info("Error while communicating with the api")
                raise UpdateFailed(f"Error communicating with API: status code was {api_result.status}")
            body = await api_result.text()
            _LOGGER.info(f"body: {body}")
            signals = json.loads(body)['signals']
            for day_data in signals:
                parsed_date = datetime.strptime(day_data['jour'], "%Y-%m-%dT%H:%M:%S%z").date()
                day_data['date'] = parsed_date

            _LOGGER.info(f"data parsed: {signals}")
            return signals
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


class EcowattLevel(CoordinatorEntity, Entity):
    """Representation of ecowatt level for a given day"""

    def __init__(self, coordinator: EcoWattAPICoordinator, days_in_advance: int):
        super().__init__(coordinator)
        self.attrs: Dict[str,Any] = {}
        self._name = f"Ecowatt level {self.day_string(date.today() + timedelta(days=days_in_advance))}"
        _LOGGER.info(f"Creating an ecowatt sensor, named {self._name}")
        self._state = None
        self.days_in_advance = days_in_advance

    def day_string(self, my_date: date):
        if my_date == date.today():
            return "today"
        elif my_date == date.today() - timedelta(days=1):
            return "tomorrow"
        else:
            return f"in {(my_date - date.today()).days} days"

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.info("checkpoint1")
        today = date.today()
        import os
        if 'ECOWATT_DEBUG' in os.environ:
            today = date(2022, 6, 6) # never commit this!
        relevant_date = today + timedelta(days=self.days_in_advance)
        _LOGGER.info(f"{self._name} {relevant_date}")
        _LOGGER.info("checkpoint2")
        ecowatt_data = next(filter(lambda e: e['date'] == relevant_date, self.coordinator.data))
        _LOGGER.info(f"{self._name} {ecowatt_data}")
        self._state = ecowatt_data['dvalue']
        _LOGGER.info("checkpoint3")
        self.attrs[ATTR_LEVEL_STRING] = ecowatt_data['message']
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return f"ecowatt-level-in-{self.days_in_advance}-days"

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def device_state_attributes(self) -> Dict[str, Any]:
        return self.attrs
