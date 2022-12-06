import os
import re
import json
import urllib.parse
import logging
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Tuple
from dateutil import tz
from itertools import dropwhile, takewhile
from oauthlib.oauth2 import BackendApplicationClient
from async_oauthlib import OAuth2Session
import aiohttp


from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.components.sensor import RestoreSensor

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENEDIS_LOAD_SHEDDING,
    TOKEN_URL,
    BASE_URL,
    ATTR_LEVEL_CODE,
    CONF_SENSOR_UNIT,
    CONF_SENSOR_SHIFT,
    CONF_SENSORS,
    ATTR_GENERATION_TIME,
    ATTR_PERIOD_START,
    ATTR_PERIOD_END,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("Called async setup entry from __init__.py")

    hass.data.setdefault(DOMAIN, {})

    # here we store the coordinator for future access
    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]["rte_coordinator"] = EcoWattAPICoordinator(
        hass, dict(entry.data)
    )
    hass.data[DOMAIN][entry.entry_id]["enedis_coordinator"] = EnedisAPICoordinator(
        hass, dict(entry.data)
    )

    # will make sure async_setup_entry from sensor.py is called
    hass.config_entries.async_setup_platforms(entry, [Platform.SENSOR])

    # subscribe to config updates
    entry.async_on_unload(entry.add_update_listener(update_entry))

    return True


async def update_entry(hass, entry):
    """
    This method is called when options are updated
    We trigger the reloading of entry (that will eventually call async_unload_entry)
    """
    _LOGGER.debug("update_entry method called")
    # will make sure async_setup_entry from sensor.py is called
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """This method is called to clean all sensors before re-adding them"""
    _LOGGER.debug("async_unload_entry method called")
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform.SENSOR]
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class AsyncOauthClient:
    def __init__(self, config):
        self.config = config
        self.token = ""

    async def client(self):
        client = BackendApplicationClient(client_id=self.config[CONF_CLIENT_ID])
        session = OAuth2Session(client=client)
        auth = aiohttp.helpers.BasicAuth(
            self.config[CONF_CLIENT_ID], self.config[CONF_CLIENT_SECRET]
        )
        self.token = await session.fetch_token(token_url=TOKEN_URL, auth=auth)
        _LOGGER.debug("Fetched a token for RTE API")
        return session


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
        self.oauth_client = AsyncOauthClient(config)

    async def async_oauth_client(self):
        client = await self.oauth_client.client()
        self.token = client.token
        return client

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)

    def skip_refresh(self) -> Optional[str]:
        """
        Returns a string describing the reason to skip data refresh or None otherwise
        """
        now = datetime.now(tz=self._timezone())
        maintenances = [
            [
                datetime(2022, 11, 30, 4, 15, 00, tzinfo=ZoneInfo("Europe/Paris")),
                timedelta(hours=1),
            ],
            [
                datetime(2022, 12, 6, 4, 15, 00, tzinfo=ZoneInfo("Europe/Paris")),
                timedelta(hours=1),
            ],
        ]
        for maintenance in maintenances:
            if now >= maintenance[0] and now < maintenance[0] + maintenance[1]:
                return (
                    f"Planned RTE API maintenance is happening until {maintenance[1]}"
                )
        return None

    async def update_method(self):
        """Fetch data from API endpoint.

        This could be the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            _LOGGER.debug(
                f"Calling update method, {len(self._listeners)} listeners subscribed"
            )
            if "ECOWATT_APIFAIL" in os.environ:
                raise UpdateFailed(
                    "Failing update on purpose to test state restoration"
                )
            _LOGGER.debug("Starting collecting data")
            if self.skip_refresh():
                _LOGGER.warning(f"Skipping data refresh because: {self.skip_refresh()}")
                return self.data
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


class RestorableCoordinatedSensor(RestoreSensor):
    @property
    def restored(self):
        return self._restored

    def restore_even_if_unknown(self):
        return False

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        _LOGGER.debug("starting to restore sensor from previous data")
        if (last_stored_state := await self._async_get_restored_data()) is not None:
            old_state = last_stored_state.state.as_dict()
            _LOGGER.debug(f"old state: {old_state}")
            if old_state["state"] != "unknown" or self.restore_even_if_unknown():
                _LOGGER.debug(f"Restoring state for {self.unique_id}")
                self._state = old_state["state"]
                for key, value in old_state["attributes"].items():
                    self._attr_extra_state_attributes[key] = value
                self.coordinator.last_update_success = True
            else:
                # by not restoring state, we allow the Coordinator to fetch data again and fill
                # data as soon as possible
                _LOGGER.debug(f"Stored state was 'unknown', starting from scratch")
        # signal restoration happened
        self._restored = True


class NextDowngradedEcowattLevel(CoordinatorEntity, RestorableCoordinatedSensor):
    """Expose next downgraded period"""

    def __init__(self, coordinator: EcoWattAPICoordinator, hass: HomeAssistant):
        super().__init__(coordinator)
        self._restored = False
        self.hass = hass
        _LOGGER.info("Creating ecowatt sensor for next downgraded period")
        self._attr_name = "Next downgraded period"
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

        if (self._state, self._attr_extra_state_attributes[ATTR_PERIOD_END]) != (
            previous_state,
            previous_end,
        ):
            _LOGGER.info(f"updated '{self.name}'")
        self.async_write_ha_state()

    @property
    def state(self) -> Optional[datetime]:
        return self._state

    @property
    def native_value(self) -> Optional[datetime]:
        return self._state

    @property
    def device_class(self) -> str:
        return "timestamp"

    def restore_even_if_unknown(self):
        # This sensor is expected to be unknown most of the time
        # it makes no sense not to restore it if its previous value was unknown
        return True

    def _find_next_downgraded_period(self) -> Optional[Tuple[datetime, datetime]]:
        now = datetime.now(self._timezone())
        if "ECOWATT_DEBUG" in os.environ:
            now = datetime(2022, 6, 3, 8, 0, 0, tzinfo=self._timezone())
        all_values = []
        for day_data in self.coordinator.data:
            for hour_data in day_data["values"]:
                date = datetime.combine(
                    day_data["date"], datetime.min.time(), tzinfo=self._timezone()
                ) + timedelta(hours=hour_data["pas"])
                all_values.append(hour_data | {"date": date})
        future_values = dropwhile(lambda data: data["date"] < now, all_values)
        first_downgraded = dropwhile(lambda data: data["hvalue"] < 2, future_values)
        all_downgraded_period = list(
            takewhile(lambda data: data["hvalue"] >= 2, first_downgraded)
        )
        if len(all_downgraded_period) == 0:
            return None
        return (
            all_downgraded_period[0]["date"],
            all_downgraded_period[-1]["date"] + timedelta(hours=1),
        )

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)


class AbstractEcowattLevel(CoordinatorEntity, RestorableCoordinatedSensor):
    """Representation of ecowatt level for a given day"""

    def __init__(
        self, coordinator: EcoWattAPICoordinator, shift: int, hass: HomeAssistant
    ):
        super().__init__(coordinator)
        self._restored = False
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

    @property
    def native_value(self):
        return self._state


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


class EnedisAPICoordinator(DataUpdateCoordinator):
    """A coordinator to fetch data from the api only once"""

    def __init__(self, hass, config: ConfigType):
        super().__init__(
            hass,
            _LOGGER,
            name="enedis api",  # for logging purpose
            update_method=self.update_method,
        )
        self.config = config
        self.hass = hass
        self._async_client = None

    async def async_client(self):
        if not self._async_client:
            self._async_client = get_async_client(self.hass, verify_ssl=True)
        return self._async_client

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)

    async def fetch_street_and_insee_code(self) -> Tuple[str,str]:
        client = await self.async_client()
        if 'ECOWATT_DEBUG' in os.environ:
            lat = 48.841
            lon = 2.3332
        else:
            lat = self.hass.config.as_dict()['latitude']
            lon = self.hass.config.as_dict()['longitude']
        r = await client.get(f"https://api-adresse.data.gouv.fr/reverse/?lat={lat}&lon={lon}")
        if not r.is_success:
            raise UpdateFailed("Failed to fetch address from api-adresse.data.gouv.fr api")
        data = r.json()
        _LOGGER.debug(f"Data received from api-adresse.data.gouv.fr: {data}")
        if len(data["features"]) == 0:
            _LOGGER.warn(f"Data received from api-adresse.data.gouv.fr is empty for those coordinates: ({lat}, {lon}). Are you sure they are located in France?")
            raise UpdateFailed("Impossible to find approximate address of the current HA instance")
        properties = data["features"][0]["properties"]
        return (properties["street"], properties["citycode"])

    async def update_method(self):
        """Fetch data from API endpoint."""
        try:
            _LOGGER.debug(
                f"Calling update method, {len(self._listeners)} listeners subscribed"
            )
            if "ENEDIS_APIFAIL" in os.environ:
                raise UpdateFailed(
                    "Failing update on purpose to test state restoration"
                )
            _LOGGER.debug("Starting collecting data")
            client = await self.async_client()

            r = await client.get(
                "https://megacache.p.web-enedis.fr/anon/v2/shedding/state_js"
            )
            if not r.is_success:
                _LOGGER.warn(
                    f"Failure to fetch data from /shedding/state_js endpoint: {r.text}"
                )
                raise UpdateFailed("Failed fetching enedis data at step 1")
            match = re.search(r"^.+var xtick0\s*=\s*'(.+)'.*$", r.text)
            if not match:
                raise UpdateFailed(f"Impossible to find expected data in {r.text}")
            step = match.groups()[0]

            r = await client.post(
                "https://megacache.p.web-enedis.fr/v2/g/trace", json={"step": step}
            )
            if not r.is_success:
                _LOGGER.warn(
                    f"Failure to fetch data from /v2/g/trace endpoint: {r.text}"
                )
                raise UpdateFailed("Failed fetching enedis data at step 2")
            jwt_token = r.json()["token"]
            _LOGGER.debug(f"Fetched token {jwt_token} from enedis api")

            (street, city_code) = await self.fetch_street_and_insee_code()
            encoded_street = urllib.parse.quote_plus(street)

            url = f"https://megacache.p.web-enedis.fr/v2/shedding?street={encoded_street}&insee_code={city_code}"
            _LOGGER.debug(f"Requesting shedding from {url}")
            r = await client.get(url,
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            if not r.is_success:
                _LOGGER.warn(f"Failure to fetch data from /v2/shedding: {r.text}")
                raise UpdateFailed("Failed fetching enedis data at step 3")
            data = r.json()
            if "ECOWATT_DEBUG" in os.environ:
                # TODO: show a real example of shedding
                data = {"success": True, "eld": False, "shedding": []}
            _LOGGER.debug(f"Data fetched from enedis: {data}")
            if not data["success"]:
                raise UpdateFailed("Enedis API answered success: false at step 4")
            for shedding_event in data["shedding"]:
                shedding_event["start_date"] = datetime.strptime(
                    shedding_event["start_date"], "%Y-%m-%dT%H:%M:%S%z"
                )
                shedding_event["stop_date"] = datetime.strptime(
                    shedding_event["stop_date"], "%Y-%m-%dT%H:%M:%S%z"
                )
                shedding_event["refresh_date"] = datetime.strptime(
                    shedding_event["refresh_date"], "%Y-%m-%dT%H:%M:%S%z"
                )
            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


class EnedisNextDowngradedPeriod(CoordinatorEntity, RestorableCoordinatedSensor):
    """Expose next downgraded period for Enedis"""

    def __init__(self, coordinator: EnedisAPICoordinator, hass: HomeAssistant):
        super().__init__(coordinator)
        self._restored = False
        self.hass = hass
        _LOGGER.info("Creating enedis sensor for next downgraded period")
        self._attr_name = "Next load shedding"
        self._state = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}

    @property
    def unique_id(self) -> str:
        return f"enedis-next-downgraded-period"

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

        if (self._state, self._attr_extra_state_attributes[ATTR_PERIOD_END]) != (
            previous_state,
            previous_end,
        ):
            _LOGGER.info(f"updated '{self.name}'")
        self.async_write_ha_state()

    @property
    def state(self) -> Optional[datetime]:
        return self._state

    @property
    def native_value(self) -> Optional[datetime]:
        return self._state

    @property
    def device_class(self) -> str:
        return "timestamp"

    def restore_even_if_unknown(self):
        # This sensor is expected to be unknown most of the time
        # it makes no sense not to restore it if its previous value was unknown
        return True

    def _find_next_downgraded_period(self) -> Optional[Tuple[datetime, datetime]]:
        now = datetime.now(self._timezone())
        if self.coordinator.data["eld"]:
            _LOGGER.warn(
                f"Current location is served by a local-distribution company (ELD). Enedis does not provide data about it"
            )
            return None

        _LOGGER.debug(
            f"Enedis returned {len(self.coordinator.data['shedding'])} shedding events"
        )

        next_shedding_event = None
        for shedding_event in self.coordinator.data["shedding"]:
            if shedding_event["stop_date"] < now:
                continue
            if (
                next_shedding_event is None
                or shedding_event["start_date"] < next_shedding_event["start_date"]
            ):
                next_shedding_event = shedding_event
        if next_shedding_event:
            return (next_shedding_event["start_date"], next_shedding_event["stop_date"])
        return None

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)

    pass

async def async_migrate_entry(hass, config_entry: ConfigEntry):
    if config_entry.version == 1:
        _LOGGER.warn(f"config_entry version is {config_entry.version}, migrating to version 2")
        new = {**config_entry.data}
        new[CONF_ENEDIS_LOAD_SHEDDING] = False
        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)
        _LOGGER.info(f"Migration to version {config_entry.version} successful")
    return True
