import voluptuous as vol
from datetime import timedelta, datetime
from typing import Any, Dict, Optional, Tuple
import logging
import asyncio

from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorEntity,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import Entity
from homeassistant.config_entries import ConfigEntry

from . import (
    EcoWattAPICoordinator,
    NextDowngradedEcowattLevel,
    HourlyEcowattLevel,
    DailyEcowattLevel,
)
from .const import (
    DOMAIN,
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
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sensors = []
    sensors.append(DailyEcowattLevel(coordinator, 0, hass))
    sensors.append(HourlyEcowattLevel(coordinator, 0, hass))
    sensors.append(NextDowngradedEcowattLevel(coordinator, hass))
    async_add_entities(sensors)
    while not all(s.restored for s in sensors):
        _LOGGER.debug("Wait for all sensors to have been restored")
        await asyncio.sleep(0.2)
    _LOGGER.debug("All sensors have been restored properly")

    # we declare update_interval after initialization to avoid a first refresh before we setup entities
    coordinator.update_interval = timedelta(minutes=16)
    # force a first refresh immediately to avoid waiting for 16 minutes
    if any(s.state is None for s in sensors):  # one sensor needs immediate refresh
        await coordinator.async_config_entry_first_refresh()
    else:
        # it means we might not get up to date info if HA was stopped for a while. TODO: detect last refresh for each sensor to take the best decision
        _LOGGER.info(
            "All sensors have already a known state, we'll wait next refresh to avoid hitting API limit after a restart"
        )
        coordinator._schedule_refresh()
    _LOGGER.info("We finished the setup of ecowatt *entity*")


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
):
    """Set up the My EcoWatt by RTE component."""

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

    while not all(s.restored for s in sensors):
        _LOGGER.debug("Wait for all sensors to have been restored")
        await asyncio.sleep(0.2)
    _LOGGER.debug("All sensors have been restored properly")

    # we declare update_interval after initialization to avoid a first refresh before we setup entities
    coordinator.update_interval = timedelta(minutes=16)
    # force a first refresh immediately to avoid waiting for 16 minutes
    if any(s.state is None for s in sensors):  # one sensor needs immediate refresh
        await coordinator.async_config_entry_first_refresh()
    else:
        # it means we might not get up to date info if HA was stopped for a while. TODO: detect last refresh for each sensor to take the best decision
        _LOGGER.info(
            "All sensors have already a known state, we'll wait next refresh to avoid hitting API limit after a restart"
        )
        coordinator._schedule_refresh()
    _LOGGER.info("We finished the setup of ecowatt platform")
