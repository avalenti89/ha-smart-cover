"""Window position sensor for Smart Cover.

Publishes the settled window position as an entity of its own, so
automations trigger on a plain state change the way they would for any
native sensor: only this entity waking them, and only when the window
actually moves. The alternative — reading it off an attribute of the
cover — makes one entity describe two different physical objects (the
shutter and the window), and drags every consumer into the cover's
constant position updates.

The cover entity keeps using the tracker's instantaneous reading for
rain protection. This sensor is the settled one.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.start import async_at_start

from .const import (
    CONF_TARGET_COVER,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    WINDOW_POSITION_OPTIONS,
    WINDOW_UNKNOWN,
)
from .window import WindowTracker


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = {**entry.data, **entry.options}
    tracker: WindowTracker = hass.data[DOMAIN][entry.entry_id]["window"]
    if not data.get(CONF_WINDOW_SENSOR):
        # Nothing to report on: without a window sensor the position is
        # permanently unknown, and an entity stuck on unknown is worse
        # than no entity at all.
        return
    async_add_entities([SmartCoverWindowSensor(entry, data, tracker)])


class SmartCoverWindowSensor(SensorEntity):
    """The window's settled position: closed, tilted (vasistas) or open."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = WINDOW_POSITION_OPTIONS
    _attr_translation_key = "window_position"
    _attr_has_entity_name = True

    def __init__(
        self, entry: ConfigEntry, data: dict[str, Any], tracker: WindowTracker
    ) -> None:
        self._entry = entry
        self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_{data[CONF_TARGET_COVER]}_window_position"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry.entry_id)})

    @property
    def native_value(self) -> str | None:
        position = self._tracker.position
        if position is None or position == WINDOW_UNKNOWN:
            return None
        return position

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Everything needed to judge a transition without going to the
        recorder: what it was before, for how long, since when it has
        been where it is, and what the sensors say at this instant."""
        tracker = self._tracker
        return {
            "previous": tracker.previous,
            "previous_duration": (
                None
                if tracker.previous_duration is None
                else round(tracker.previous_duration)
            ),
            "since": None if tracker.since is None else tracker.since.isoformat(),
            # The instantaneous reading, before the debounce has had its
            # say — the same one rain protection acts on.
            "raw": tracker.raw_position,
            "raw_since": (
                None if tracker.raw_since is None else tracker.raw_since.isoformat()
            ),
            "debounce": tracker.debounce,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._tracker.add_listener(self._async_tracker_updated))
        # The tracker takes its first reading only once Home Assistant
        # has started (see WindowTracker.async_started); until then this
        # entity would publish a position nobody has actually measured.
        self.async_on_remove(async_at_start(self.hass, self._async_handle_started))

    async def _async_handle_started(self, _hass: HomeAssistant) -> None:
        self.async_write_ha_state()

    @callback
    def _async_tracker_updated(self) -> None:
        self.async_write_ha_state()
