import voluptuous as vol
from datetime import timedelta, datetime
import logging
import asyncio

from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from . import (
    EcoWattAPICoordinator,
    DowngradedEcowattLevelCalendar,
)
from .const import (
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    _LOGGER.info("Called async setup entry for calendar")
    rte_coordinator = hass.data[DOMAIN][entry.entry_id]["rte_coordinator"]
    sensors = []
    sensors.append(DowngradedEcowattLevelCalendar(rte_coordinator, hass))

    async_add_entities(sensors)

    _LOGGER.info("We finished the setup of ecowatt *calendar*")
