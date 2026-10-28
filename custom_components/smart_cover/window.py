"""Window position tracking for Smart Cover.

One tracker per config entry, and the only place in this integration
that reads the window sensors. The cover entity asks it which rain cap
applies; the window sensor entity publishes what it settles on. Neither
re-derives the classification, so there is exactly one definition of
"what position is this window in" to keep correct.

Two readings are kept side by side, on purpose:

- RAW is instantaneous. Rain protection uses it: a window opening onto
  the rain has to be acted on the moment it happens, not after a delay.
- SETTLED only adopts a position once it has held for `debounce`
  seconds. Anything shorter never becomes settled and leaves no trace
  in the history of previous positions.

That second reading is what makes a transition legible. Going from
vasistas to fully open on these windows requires shutting the sash for
a few seconds first, so the raw sequence reads
"tilted -> closed -> open" and the middle step is indistinguishable
from someone actually closing the window. Debounced, the same
manoeuvre reads "tilted -> open" with the tilted duration intact, and a
reed contact bouncing under a window that is already open produces no
transition at all.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import (
    CONF_TILT_CLOSED_VALUE,
    CONF_TILT_SENSOR,
    CONF_TILT_TOLERANCE,
    CONF_WINDOW_DEBOUNCE,
    CONF_WINDOW_SENSOR,
    DEFAULT_TILT_CLOSED_VALUE,
    DEFAULT_TILT_TOLERANCE,
    DEFAULT_WINDOW_DEBOUNCE,
    DOMAIN,
    WINDOW_CLOSED,
    WINDOW_OPEN,
    WINDOW_TILTED,
    WINDOW_UNKNOWN,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Written through a delayed save: a window that is being worked on
# produces a burst of changes, and each one does not need its own disk
# write.
STORAGE_SAVE_DELAY = 10

# How long a configured sensor has to stay missing or unavailable before
# it is reported as a repair issue. Long enough that a battery sensor
# missing a few advertisements, or an integration still starting up,
# does not raise one.
SENSOR_PROBLEM_GRACE = 900

UNAVAILABLE_STATES = ("unknown", "unavailable")


class WindowTracker:
    """Classifies one window and remembers how it got there."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any]
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._name: str = data.get("name") or entry.title
        self._window_sensor: str | None = data.get(CONF_WINDOW_SENSOR) or None
        self._tilt_sensor: str | None = data.get(CONF_TILT_SENSOR) or None
        # Upper edge of the "sash vertical" band: above it is vasistas.
        self._tilt_vasistas_above: float = float(
            data.get(CONF_TILT_CLOSED_VALUE, DEFAULT_TILT_CLOSED_VALUE)
        ) + abs(float(data.get(CONF_TILT_TOLERANCE, DEFAULT_TILT_TOLERANCE)))
        self.debounce: int = int(
            data.get(CONF_WINDOW_DEBOUNCE, DEFAULT_WINDOW_DEBOUNCE)
        )

        # Settled view. previous_duration is in seconds.
        self.position: str | None = None
        self.since: datetime | None = None
        self.previous: str | None = None
        self.previous_duration: float | None = None
        # When the raw reading last changed — useful for telling "just
        # moved" from "has been like this for hours" before the debounce
        # has had its say.
        self.raw_since: datetime | None = None

        self._last_raw: str | None = None
        self._pending: str | None = None
        self._cancel_promotion: Callable[[], None] | None = None
        self._cancel_problem: Callable[[], None] | None = None
        self._problem_active = False
        # Set by async_started: until then, sensor changes are ignored
        # (see _async_sensor_changed).
        self._started = False
        self._listeners: list[Callable[[], None]] = []
        self._unsub_state: Callable[[], None] | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.window"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Restore what was known before, then start tracking."""
        await self._async_restore()

        watched = [s for s in (self._window_sensor, self._tilt_sensor) if s]
        if watched:
            self._unsub_state = async_track_state_change_event(
                self.hass, watched, self._async_sensor_changed
            )

    @callback
    def async_started(self) -> None:
        """Take the first reading, once Home Assistant has fully started.

        Deliberately not done in async_start: at setup time a Bluetooth
        or battery-powered sensor may not have reported yet, and reading
        it then would settle the window on "unknown" and immediately
        record a bogus transition out of it.
        """
        if self._started:
            # async_at_start fires once per registration, but a reload
            # re-registers it: seeding twice would record a transition
            # out of a position the window never left.
            return
        self._started = True

        raw = self.raw_position
        self._last_raw = raw
        self.raw_since = dt_util.utcnow()

        if self.position is None:
            # Nothing restored (first run, or storage cleared): adopt
            # what the window reads now, with no previous position to
            # speak of.
            self.position = raw
            self.since = dt_util.utcnow()
        elif self.position != raw:
            # It moved while Home Assistant was down. Treat it as a
            # transition that has already happened rather than pretending
            # the restored position is still current.
            self._promote(raw)
        # If the restored position still matches, `since` is kept as it
        # was: a restart must not reset how long the window has been
        # where it is.

        self._schedule_save()
        self._check_sensors()
        self._notify()

    @callback
    def async_stop(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._cancel_pending()
        if self._cancel_problem is not None:
            self._cancel_problem()
            self._cancel_problem = None

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to settled-position changes; returns the unsubscribe."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    @property
    def raw_position(self) -> str:
        """The window's position right now, straight off the sensors.

        The open/close sensor decides open vs closed and is read first;
        the rotation sensor only distinguishes vasistas from a fully open
        sash: vasistas is any rotation above the vertical reading plus
        its tolerance. A rotation while the sash reads closed is not a
        state these windows can physically be in, so it is not treated
        as one.
        """
        if not self._window_sensor:
            return WINDOW_UNKNOWN
        window_state = self.hass.states.get(self._window_sensor)
        if window_state is None or window_state.state in UNAVAILABLE_STATES:
            return WINDOW_UNKNOWN
        if window_state.state == "off":
            return WINDOW_CLOSED
        if self._tilt_sensor:
            tilt_state = self.hass.states.get(self._tilt_sensor)
            if tilt_state is not None:
                try:
                    if float(tilt_state.state) > self._tilt_vasistas_above:
                        return WINDOW_TILTED
                except (TypeError, ValueError):
                    pass
        return WINDOW_OPEN

    @property
    def is_closed(self) -> bool | None:
        """True/False on the settled position, None while unknown.

        The one question consumers outside the rain logic actually ask:
        a vasistas window is open, because air goes through it.
        """
        if self.position is None or self.position == WINDOW_UNKNOWN:
            return None
        return self.position == WINDOW_CLOSED

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

    @callback
    def _async_sensor_changed(self, _event: Event[EventStateChangedData]) -> None:
        if not self._started:
            # Tracking begins at setup, but the first reading is only
            # taken once Home Assistant has started (async_started).
            # Acting on changes in between would race that seeding: the
            # debounce would promote a position while there is still no
            # position to compare it against, and the transition it
            # recorded would be one nobody made.
            return
        raw = self.raw_position
        if raw != self._last_raw:
            self._last_raw = raw
            self.raw_since = dt_util.utcnow()
        self._evaluate(raw)
        self._check_sensors()
        self._notify()

    @callback
    def _cancel_pending(self) -> None:
        if self._cancel_promotion is not None:
            self._cancel_promotion()
            self._cancel_promotion = None
        self._pending = None

    @callback
    def _evaluate(self, raw: str) -> None:
        if raw == self.position:
            # Back where it started before the debounce elapsed: as far
            # as consumers are concerned the excursion never happened.
            self._cancel_pending()
            return
        if raw == self._pending:
            # Same candidate still waiting — leave its timer alone rather
            # than restarting it on an unrelated sensor update.
            return
        self._cancel_pending()
        if self.debounce <= 0:
            self._promote(raw)
            self._schedule_save()
            return
        self._pending = raw
        self._cancel_promotion = async_call_later(
            self.hass, self.debounce, self._async_debounce_elapsed
        )

    @callback
    def _async_debounce_elapsed(self, _now: datetime) -> None:
        self._cancel_promotion = None
        pending = self._pending
        if pending is None:
            return
        self._promote(pending)
        self._schedule_save()
        self._notify()

    @callback
    def _promote(self, position: str) -> None:
        if position == self.position:
            # Not a transition. Recording one anyway would publish the
            # nonsense "it was open for 7 seconds, and now it is open",
            # and would reset `since` — losing how long the window has
            # actually been where it is, which is the one thing
            # consumers rely on.
            self._pending = None
            return
        now = dt_util.utcnow()
        if self.position is not None:
            self.previous = self.position
            self.previous_duration = (
                None if self.since is None else (now - self.since).total_seconds()
            )
        self.position = position
        self.since = now
        self._pending = None
        _LOGGER.debug(
            "%s: window settled on %s (was %s for %ss)",
            self._name,
            position,
            self.previous,
            None
            if self.previous_duration is None
            else round(self.previous_duration),
        )

    # ------------------------------------------------------------------
    # Sensor health
    #
    # A window sensor that disappears is not a crash: the classification
    # falls back to "unknown" and the rain logic already answers that
    # with its strictest cap. But it does leave every consumer quietly
    # working off a window it can no longer see, so it is reported the
    # way a native integration would — as a repair issue, raised only
    # once the sensor has been gone long enough to rule out a hiccup.
    # ------------------------------------------------------------------

    @callback
    def _missing_sensors(self) -> list[str]:
        missing = []
        for entity_id in (self._window_sensor, self._tilt_sensor):
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in UNAVAILABLE_STATES:
                missing.append(entity_id)
        return missing

    @callback
    def _check_sensors(self) -> None:
        missing = self._missing_sensors()
        if not missing:
            if self._cancel_problem is not None:
                self._cancel_problem()
                self._cancel_problem = None
            if self._problem_active:
                self._problem_active = False
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
                _LOGGER.info("%s: window sensors are back", self._name)
            return
        if self._problem_active or self._cancel_problem is not None:
            return
        self._cancel_problem = async_call_later(
            self.hass, SENSOR_PROBLEM_GRACE, self._async_report_problem
        )

    @property
    def _issue_id(self) -> str:
        return f"window_sensor_unavailable_{self._entry.entry_id}"

    @callback
    def _async_report_problem(self, _now: datetime) -> None:
        self._cancel_problem = None
        missing = self._missing_sensors()
        if not missing:
            return
        self._problem_active = True
        _LOGGER.warning(
            "%s: window sensor(s) unavailable for over %s minutes: %s. The window "
            "position reads as unknown and the strictest rain limit applies.",
            self._name,
            SENSOR_PROBLEM_GRACE // 60,
            ", ".join(missing),
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="window_sensor_unavailable",
            translation_placeholders={
                "name": self._name,
                "entities": ", ".join(missing),
            },
        )

    # ------------------------------------------------------------------
    # Persistence
    #
    # Kept in the integration's own store rather than restored from the
    # sensor entity: the tracker is the source of truth, and it has to
    # answer before its entities exist. Durations are what make this
    # worth persisting — without it, every restart resets "the window
    # has been closed since last night" to "since a moment ago".
    # ------------------------------------------------------------------

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "since": None if self.since is None else self.since.isoformat(),
            "previous": self.previous,
            "previous_duration": self.previous_duration,
        }

    async def _async_restore(self) -> None:
        stored = await self._store.async_load()
        if not stored:
            return
        self.position = stored.get("position")
        self.previous = stored.get("previous")
        self.previous_duration = stored.get("previous_duration")
        since = stored.get("since")
        if since:
            self.since = dt_util.parse_datetime(since)
