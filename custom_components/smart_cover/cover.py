"""Cover platform for Smart Cover.

Home Assistant forwards to this file (custom_components/smart_cover/cover.py)
via config_entries.async_forward_entry_setups(entry, ["cover"]) — see
__init__.py. This is just the platform entry point: it creates the
entity (SmartCoverEntity, in entity.py) and registers the custom
`smart_cover.*` services on it.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_ENABLED,
    ATTR_END_TIME,
    ATTR_POSITION,
    ATTR_START_POSITION,
    ATTR_STEPS,
    DEFAULT_RAMP_STEPS,
    DOMAIN,
    SERVICE_ACTIVATE_RAIN_PROTECTION,
    SERVICE_CLOSE_COMPLETELY,
    SERVICE_RAMP_TO_POSITION,
    SERVICE_RECALIBRATE_MANUAL,
    SERVICE_REQUEST_POSITION,
    SERVICE_REQUEST_RECALIBRATION,
    SERVICE_SET_RAIN_CONTROL_ENABLED,
    SERVICE_SET_RECALIBRATION_ENABLED,
)
from .entity import SmartCoverEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = {**entry.data, **entry.options}
    # The window tracker is created in __init__.py before the platforms,
    # so the cover and the window sensor share one instance.
    window = hass.data[DOMAIN][entry.entry_id]["window"]
    async_add_entities([SmartCoverEntity(hass, entry, data, window)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_RECALIBRATE_MANUAL, {}, "async_recalibrate_manual"
    )
    platform.async_register_entity_service(
        SERVICE_REQUEST_RECALIBRATION, {}, "async_request_recalibration"
    )
    platform.async_register_entity_service(
        SERVICE_REQUEST_POSITION,
        {
            vol.Required(ATTR_POSITION): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
        },
        "async_request_position",
    )
    platform.async_register_entity_service(
        SERVICE_SET_RECALIBRATION_ENABLED,
        {vol.Required(ATTR_ENABLED): bool},
        "async_set_recalibration_enabled",
    )
    platform.async_register_entity_service(
        SERVICE_SET_RAIN_CONTROL_ENABLED,
        {vol.Required(ATTR_ENABLED): bool},
        "async_set_rain_control_enabled",
    )
    platform.async_register_entity_service(
        SERVICE_ACTIVATE_RAIN_PROTECTION, {}, "async_activate_rain_protection"
    )
    platform.async_register_entity_service(
        SERVICE_CLOSE_COMPLETELY, {}, "async_close_completely"
    )
    platform.async_register_entity_service(
        SERVICE_RAMP_TO_POSITION,
        {
            vol.Required(ATTR_POSITION): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
            vol.Optional(ATTR_START_POSITION): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
            vol.Required(ATTR_END_TIME): cv.datetime,
            vol.Optional(ATTR_STEPS, default=DEFAULT_RAMP_STEPS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=120)
            ),
        },
        "async_ramp_to_position",
    )
