import voluptuous as vol
from datetime import timedelta, datetime
from typing import Any, Dict, Optional, Tuple
import logging
import json
import os
from dateutil import tz
from itertools import dropwhile, takewhile

from oauthlib.oauth2 import BackendApplicationClient
from async_oauthlib import OAuth2Session
import aiohttp

from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (
        PLATFORM_SCHEMA,
        SensorEntity,
        )
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    TOKEN_URL,
    BASE_URL,
    ATTR_LEVEL_CODE,
    CONF_SENSOR_UNIT,
    CONF_SENSOR_SHIFT,
    CONF_SENSORS,
    ATTR_GENERATION_TIME,
    ATTR_PERIOD_START,
    ATTR_PERIOD_END,
    ATTR_PERIOD_DURATION
)

_LOGGER = logging.getLogger(__name__)

SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SENSOR_UNIT): vol.All(cv.string, vol.In(["days", "hours"])),
        vol.Required(CONF_SENSOR_SHIFT): vol.Coerce(int),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Required(CONF_SENSORS, default=[]): vol.All(
            cv.ensure_list, [SENSOR_SCHEMA]
        ),
    }
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    _LOGGER.info("Called async setup entry")


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
):
    """Set up the My EcoWatt by RTE component."""

    # get a token
    coordinator = EcoWattAPICoordinator(hass, config)

    sensors = []
    sensors.append(DailyEcowattLevel(coordinator, 0, hass))
    sensors.append(HourlyEcowattLevel(coordinator, 0, hass))
    sensors.append(NextDowngradedEcowattLevel(coordinator, hass))
    for sensor_config in config[CONF_SENSORS]:
        if sensor_config[CONF_SENSOR_UNIT] == "days":
            klass = DailyEcowattLevel
        elif sensor_config[CONF_SENSOR_UNIT] == "hours":
            klass = HourlyEcowattLevel
        else:
            assert (
                False
            ), f"{sensor_config[CONF_SENSOR_UNIT]} is invalid, it should have been caught at configuration validation"
        sensors.append(klass(coordinator, sensor_config[CONF_SENSOR_SHIFT], hass))

    async_add_entities(sensors)
    # force a first refresh immediately to avoid waiting for 16 minutes
    coordinator.schedule_interval = timedelta(minutes=16)
    await coordinator.async_config_entry_first_refresh()
    # trigger refresh *after* instanciating sensors that subscribe to it
    await coordinator.async_refresh()
    _LOGGER.info("We finished the setup of ecowatt platform")


class EcoWattAPICoordinator(DataUpdateCoordinator):
    """A coordinator to fetch data from the api only once"""

    def __init__(self, hass, config: ConfigType):
        super().__init__(
            hass,
            _LOGGER,
            name="ecowatt api",  # for logging purpose
            update_method=self.update_method,
        )
        self.config = config
        self.hass = hass

    async def async_oauth_client(self):
        client = BackendApplicationClient(client_id=self.config[CONF_CLIENT_ID])
        session = OAuth2Session(client=client)
        auth = aiohttp.helpers.BasicAuth(
            self.config[CONF_CLIENT_ID], self.config[CONF_CLIENT_SECRET]
        )
        self.token = await session.fetch_token(token_url=TOKEN_URL, auth=auth)
        _LOGGER.debug("Fetched a token for RTE API")
        return session

    async def update_method(self):
        """Fetch data from API endpoint.

        This could be the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            _LOGGER.debug("Starting collecting data")
            client = await self.async_oauth_client()
            headers = {
                "Authorization": f"{self.token['token_type']} {self.token['access_token']}"
            }
            url = f"{BASE_URL}/open_api/ecowatt/v4/signals"
            if "ECOWATT_DEBUG" in os.environ:
                url = f"{BASE_URL}/open_api/ecowatt/v4/sandbox/signals"
            api_result = await client.get(url, headers=headers)
            _LOGGER.info(f"data received, status code: {api_result.status}")
            if api_result.status == 429:
                # a code 429 is expected when requesting more often than every 15minutes and not using the sandbox url
                # FIXME(kamaradclimber): avoid this error when home assistant is restarting by storing state and last update
                raise UpdateFailed(
                    f"Error communicating with RTE API: requests too frequent to RTE API"
                )
            elif api_result.status != 200:
                raise UpdateFailed(
                    f"Error communicating with RTE API: status code was {api_result.status}"
                )
            body = await api_result.text()
            await client.close()  # we won't need the client anymore
            _LOGGER.debug(f"api response body: {body}")
            signals = json.loads(body)["signals"]
            for day_data in signals:
                parsed_date = datetime.strptime(
                    day_data["jour"], "%Y-%m-%dT%H:%M:%S%z"
                ).date()
                day_data["date"] = parsed_date

            _LOGGER.debug(f"data parsed: {signals}")
            return signals
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")

class NextDowngradedEcowattLevel(CoordinatorEntity, SensorEntity):
    """Expose next downgraded period"""

    def __init__(
            self, coordinator: EcoWattAPICoordinator, hass: HomeAssistant
    ):
        super().__init__(coordinator)
        self.hass = hass
        _LOGGER.info("Creating ecowatt sensor for next downgraded period")
        self._name = "Next downgraded period"
        self._state = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}

    @property
    def unique_id(self) -> str:
        return f"ecowatt-next-downgraded-period"

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        optional_period = self._find_next_downgraded_period()
        if not optional_period:
            optional_period = (None, None)
        (period_start, period_end) = optional_period
        previous_state = self._state
        previous_end = self._attr_extra_state_attributes.get(ATTR_PERIOD_END, None)
        self._state = period_start
        self._attr_extra_state_attributes[ATTR_PERIOD_END] = period_end

        if (self._state, self._attr_extra_state_attributes[ATTR_PERIOD_END]) != (previous_state, previous_end):
            _LOGGER.info(f"updated '{self.name}'")
        self.async_write_ha_state()

    @property
    def state(self) -> Optional[datetime]:
        return self._state

    @property
    def native_value(self) -> Optional[datetime]:
        return self._state

    def _find_next_downgraded_period(self) -> Optional[Tuple[datetime, datetime]]:
        now = datetime.now(self._timezone())
        if "ECOWATT_DEBUG" in os.environ:
            now = datetime(2022, 6, 3, 8, 0, 0, tzinfo=self._timezone())
        all_values = []
        for day_data in self.coordinator.data:
            for hour_data in day_data['values']:
                date = datetime.combine(day_data['date'], datetime.min.time(), tzinfo=self._timezone()) + timedelta(hours=hour_data['pas'])
                all_values.append(hour_data | {'date': date})
        future_values = dropwhile(lambda data: data['date'] < now, all_values)
        first_downgraded = dropwhile(lambda data: data['hvalue'] < 2, future_values)
        all_downgraded_period = list(takewhile(lambda data: data['hvalue'] >= 2, first_downgraded))
        if len(all_downgraded_period) == 0:
            return None
        return (all_downgraded_period[0]['date'], all_downgraded_period[-1]['date'] + timedelta(hours=1))

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)



class AbstractEcowattLevel(CoordinatorEntity, Entity):
    """Representation of ecowatt level for a given day"""

    def __init__(
        self, coordinator: EcoWattAPICoordinator, shift: int, hass: HomeAssistant
    ):
        super().__init__(coordinator)
        self.hass = hass
        self._attr_extra_state_attributes: Dict[str, Any] = {}
        _LOGGER.info(f"Creating an ecowatt sensor, named {self.name}")
        self._state = None
        self.shift = shift
        self.happening_now = False

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)

    def _find_ecowatt_level(self) -> int:
        raise NotImplementedError()

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        ecowatt_level = self._find_ecowatt_level()
        previous_level = self._attr_extra_state_attributes.get(ATTR_LEVEL_CODE, None)
        self._attr_extra_state_attributes[ATTR_LEVEL_CODE] = ecowatt_level
        self._state = self._level2string(ecowatt_level)
        self._attr_icon = self._level2icon(ecowatt_level)
        if previous_level != self._attr_extra_state_attributes[ATTR_LEVEL_CODE]:
            _LOGGER.info(f"updated '{self.name}' with level {self._state}")
        self.async_write_ha_state()

    def _level2string(self, level):
        if self.happening_now and level == 3:
            return "Coupure d'électricité en cours"
        return {
            1: "Situation normale",
            2: "Risques de coupures d'électricité",
            3: "Coupures d'électricité programmées",
        }[level]

    def _level2icon(self, level):
        return {
            1: "mdi:check-circle",
            2: "mdi:alert",
            3: "mdi:power-plug-off",
        }[level]

    @property
    def state(self) -> Optional[str]:
        return self._state

    def _day_string(self, day_shift):
        if day_shift == 0:
            return "today"
        elif day_shift == 1:
            return "tomorrow"
        else:
            return f"in {day_shift} days"


class HourlyEcowattLevel(AbstractEcowattLevel):
    def __init__(self, coordinator, shift: int, hass: HomeAssistant):
        days = shift // 24
        hours = shift % 24
        self._attr_name = f"Ecowatt level {self._day_string(days)} and {hours} hours"
        if shift == 0:
            self._attr_name = "Ecowatt level now"
        super().__init__(coordinator, shift=shift, hass=hass)
        if shift == 0:  # this needs to happen after initialization of super
            self.happening_now = True

    @property
    def unique_id(self) -> str:
        return f"ecowatt-level-in-{self.shift}-hours"

    def _find_ecowatt_level(self) -> int:
        now = datetime.now(self._timezone())
        if "ECOWATT_DEBUG" in os.environ:
            now = datetime(2022, 6, 3, 8, 0, 0, tzinfo=self._timezone())
        date_shift = self.shift // 24
        hour_shift = self.shift % 24
        relevant_date = now + timedelta(days=date_shift, hours=hour_shift)
        _LOGGER.debug(f"Looking for {relevant_date}")
        ecowatt_data = None
        try:
            ecowatt_data = next(
                filter(
                    lambda e: e["date"] == relevant_date.date(), self.coordinator.data
                )
            )
            self._attr_extra_state_attributes[ATTR_GENERATION_TIME] = ecowatt_data[
                "GenerationFichier"
            ]
            self._attr_extra_state_attributes[
                ATTR_PERIOD_START
            ] = relevant_date - timedelta(
                minutes=relevant_date.minute, seconds=relevant_date.second
            )
            self._attr_extra_state_attributes[
                ATTR_PERIOD_END
            ] = self._attr_extra_state_attributes[ATTR_PERIOD_START] + timedelta(
                hours=1
            )
            level = next(
                filter(lambda e: e["pas"] == relevant_date.hour, ecowatt_data["values"])
            )
            return level["hvalue"]
        except StopIteration:
            _LOGGER.info(f"Data for relevant day: {ecowatt_data}")
            raise RuntimeError(
                f"Unable to find ecowatt level for {relevant_date} (hour shift: {hour_shift})"
            )


class DailyEcowattLevel(AbstractEcowattLevel):
    def __init__(self, coordinator, shift: int, hass: HomeAssistant):
        self._attr_name = f"Ecowatt level {self._day_string(shift)}"
        super().__init__(coordinator, shift=shift, hass=hass)

    @property
    def unique_id(self) -> str:
        return f"ecowatt-level-in-{self.shift}-days"

    def _find_ecowatt_level(self) -> int:
        now = datetime.now(self._timezone())
        if "ECOWATT_DEBUG" in os.environ:
            now = datetime(2022, 6, 3, 8, 0, 0, tzinfo=self._timezone())
        relevant_date = now + timedelta(days=self.shift)
        try:
            ecowatt_data = next(
                filter(
                    lambda e: e["date"] == relevant_date.date(), self.coordinator.data
                )
            )
            self._attr_extra_state_attributes[ATTR_GENERATION_TIME] = ecowatt_data[
                "GenerationFichier"
            ]
            self._attr_extra_state_attributes[
                ATTR_PERIOD_START
            ] = relevant_date - timedelta(
                hours=relevant_date.hour,
                minutes=relevant_date.minute,
                seconds=relevant_date.second,
            )
            self._attr_extra_state_attributes[
                ATTR_PERIOD_END
            ] = self._attr_extra_state_attributes[ATTR_PERIOD_START] + timedelta(days=1)
            return ecowatt_data["dvalue"]
        except StopIteration:
            raise RuntimeError(
                f"Unable to find ecowatt level for {relevant_date.date()}"
            )
