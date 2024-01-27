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


from homeassistant.helpers.storage import Store
from homeassistant.const import Platform, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.components.sensor import RestoreSensor
from homeassistant.components.calendar import CalendarEntity, CalendarEvent

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

    # will make sure async_setup_entry from sensor.py is called
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform.SENSOR, Platform.CALENDAR]
    )

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
        entry, [Platform.SENSOR, Platform.CALENDAR]
    )
    if unload_ok:
        config = hass.data[DOMAIN].pop(entry.entry_id)
        if "rte_coordinator" in config:
            await config["rte_coordinator"].clean()
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
        self.api_version = "v5"

        self._custom_store = Store(hass, 1, "rte_ecowatt")

    async def clean(self):
        await self._custom_store.async_remove()

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
        body = None
        loaded_from_cache = False
        try:
            previous_data = await self._custom_store.async_load()
            if previous_data is not None:
                last_update = datetime.strptime(
                    previous_data["last_update"], "%Y-%m-%dT%H:%M:%S.%f"
                )
                if (datetime.now() - last_update) < timedelta(minutes=15):
                    _LOGGER.info(
                        "Loading RTE ecowatt data from storage instead of querying api"
                    )
                    body = previous_data["body"]
                    loaded_from_cache = True
        except Exception as e:
            _LOGGER.warn(f"Impossible to load previous data: {e}")
        if body is None:
            body = await self._real_update_method()
            try:
                await self._custom_store.async_save(
                    {
                        "last_update": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
                        "body": body,
                    }
                )
            except Exception as e:
                _LOGGER.exception("Error received while caching API data")
                raise e
        json_body = json.loads(body)
        if "signals" not in json_body:
            if loaded_from_cache:
                raise Exception(f"Cached data is invalid. Removing `{self._custom_store.path}` and restart HA should help")
            else:
                raise Exception(f"Fetched data is invalid, it does not have 'signals' key. {body}")
        signals = json_body["signals"]
        # additional parsing
        for day_data in signals:
            parsed_time = datetime.strptime(day_data["jour"], "%Y-%m-%dT%H:%M:%S%z")
            day_data["date"] = parsed_time.date()
            day_data["datetime"] = parsed_time

        _LOGGER.debug(f"data parsed: {signals}")
        return signals

    async def _real_update_method(self):
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
            url = f"{BASE_URL}/open_api/ecowatt/{self.api_version}/signals"
            if "ECOWATT_DEBUG" in os.environ:
                url = f"{BASE_URL}/open_api/ecowatt/{self.api_version}/sandbox/signals"
            api_result = await client.get(url, headers=headers)
            _LOGGER.info(f"data received, status code: {api_result.status}")
            if api_result.status == 403:
                # a code 403 is likely to be an api key from a previous version of the api
                if self.api_version == "v5":
                    _LOGGER.warn(
                        f"Received a 403, api key is likely from a previous version, downgrading to api v4. You can regenerate a new api key to avoid this warning"
                    )
                    self.api_version = "v4"
                    return await self.update_method()
                else:
                    raise UpdateFailed(
                        f"Error communicating with RTE API: received a 403 from api, even with version {self.api_version}"
                    )
            if api_result.status == 429:
                # a code 429 is expected when requesting more often than every 15minutes and not using the sandbox url
                # FIXME(kamaradclimber): avoid this error when home assistant is restarting by storing state and last update
                if self.data is not None:
                    _LOGGER.warn(
                        "Error communicating with RTE API: requests too frequent. Using old data"
                    )
                    return self.data
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
            return body
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

        if (extra_stored_data := await self.async_get_last_sensor_data()) is not None:
            if (
                extra_stored_data.native_value != "unknown"
                or self.restore_even_if_unknown()
            ):
                _LOGGER.debug(f"Restoring state for {self.unique_id}")
                self._attr_native_value = extra_stored_data.native_value
                self._attr_native_unit_of_measurement = (
                    extra_stored_data.native_unit_of_measurement
                )
                # sadly it seems as of 2023.6 we don't have any way to get back the additional attributes
                self.coordinator.last_update_success = True
            else:
                # by not restoring state, we allow the Coordinator to fetch data again and fill
                # data as soon as possible
                _LOGGER.debug(f"Stored state was 'unknown', starting from scratch")
        # signal restoration happened
        self._restored = True


class DowngradedEcowattLevelCalendar(CoordinatorEntity, CalendarEntity):
    def __init__(self, coordinator: EcoWattAPICoordinator, hass: HomeAssistant):
        CoordinatorEntity.__init__(self, coordinator)
        self.hass = hass
        self._attr_name = "Ecowatt downgraded level"
        self._events = []

    @property
    def event(self) -> Optional[CalendarEvent]:
        if len(self._events) > 0:
            self._events[0]
        return None

    @property
    def unique_id(self) -> str:
        return f"ecowatt-downgraded-events"

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        relevant_events = []
        for event in self._events:
            included = event.start <= start_date and event.end >= end_date
            included = included or (
                event.start >= start_date and event.start <= end_date
            )
            included = included or (event.end >= start_date and event.end <= end_date)
            if included:
                relevant_events.append(event)
        return relevant_events

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        events = []
        for event in self.coordinator.data:
            day_start = event["datetime"]
            for hour in event["values"]:
                start = day_start + timedelta(hours=hour["pas"])
                if hour["hvalue"] > 1:
                    events.append(
                        CalendarEvent(
                            start=start,
                            end=start + timedelta(hours=1),
                            summary=self._level2string(hour["hvalue"]),
                            description=f"Le niveau ecowatt prévu est {hour['hvalue']}",
                        )
                    )

        self._events = self._merge_events(events)
        self.async_write_ha_state()  # we probably don't need this line

    def _merge_events(self, events: list[CalendarEvent]) -> list[CalendarEvent]:
        events.sort(key=lambda e: e.start)
        if len(events) == 0:
            return []
        merged_events = [events[0]]
        for event in events[1:]:
            if (
                event.start == merged_events[-1].end
                and event.summary == merged_events[-1].summary
                and event.description == merged_events[-1].description
            ):
                merged_events[-1].end = event.end
            else:
                merged_events.append(event)
        return merged_events

    def _level2string(self, level):
        if self.state == STATE_ON and level == 3:
            return "Coupure d'électricité en cours"
        return {
            0: "Situation normale - Electricité faible émission de CO2",
            1: "Situation normale",
            2: "Risques de coupures d'électricité",
            3: "Coupures d'électricité programmées",
        }[level]

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, "RTE")}, "name": "RTE"}


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

    def _find_ecowatt_level(self) -> Optional[int]:
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

    def _level2string(self, level) -> Optional[str]:
        if self.happening_now and level == 3:
            return "Coupure d'électricité en cours"
        if level is None:
            return None
        return {
            0: "Situation normale - Electricité faible émission de CO2",
            1: "Situation normale",
            2: "Risques de coupures d'électricité",
            3: "Coupures d'électricité programmées",
        }[level]

    def _level2icon(self, level) -> Optional[str]:
        return {
            0: "mdi:check-circle",
            1: "mdi:check-circle",
            2: "mdi:alert",
            3: "mdi:power-plug-off",
            None: "mdi:help",
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

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, "RTE")}, "name": "RTE"}


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

    def _find_ecowatt_level(self) -> Optional[int]:
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

    def _find_ecowatt_level(self) -> Optional[int]:
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
            if self.shift >= 3:
                _LOGGER.info(
                    f"No info (yet) for D+{self.shift} level. This state might last for a few hours, most likely until 8am Paris time"
                )
                return None
            raise RuntimeError(
                f"Unable to find ecowatt level for {relevant_date.date()}"
            )


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    if config_entry.version == 1:
        _LOGGER.warn(
            f"config_entry version is {config_entry.version}, migrating to version 2"
        )
        new = {**config_entry.data}
        new["enedis_load_shedding"] = [False]
        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)
        _LOGGER.info(f"Migration to version {config_entry.version} successful")
    if config_entry.version < 3:
        _LOGGER.warn(
            f"config_entry version is {config_entry.version}, migrating to version 3"
        )
        new = {**config_entry.data}
        del new["enedis_load_shedding"]
        config_entry.version = 3
        hass.config_entries.async_update_entry(config_entry, data=new)
        _LOGGER.info(f"Migration to version {config_entry.version} successful")
    return True
