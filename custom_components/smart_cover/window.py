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
tilted to fully open on a tilt-and-turn window requires shutting the sash for
a few seconds first, so the raw sequence reads
"tilted -> closed -> open" and the middle step is indistinguishable
from someone actually closing the window. Debounced, the same
manoeuvre reads "tilted -> open" with the tilted duration intact, and a
reed contact bouncing under a window that is already open produces no
transition at all.

With window_manual on, the sensors are not read at all: the position is
the one last set by hand (set_manual_position), adopted at once with no
debounce, since nobody sets it by accident. That is the way out for a
window whose sensor is broken or missing.

It also watches the sensors' health: one that goes unavailable or silent
is reported as a repair issue and through the notifications configured
for the cover, and withdrawn when it comes back (see "Sensor health").
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import translation
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .const import (
    CONF_NOTIFY_PERSISTENT,
    CONF_SENSOR_SILENCE_HOURS,
    CONF_TILT_CLOSED_VALUE,
    CONF_TILT_SENSOR,
    CONF_TILT_TOLERANCE,
    CONF_WINDOW_DEBOUNCE,
    CONF_WINDOW_MANUAL,
    CONF_WINDOW_SENSOR,
    DEFAULT_NOTIFY_PERSISTENT,
    DEFAULT_SENSOR_SILENCE_HOURS,
    DEFAULT_TILT_CLOSED_VALUE,
    DEFAULT_TILT_TOLERANCE,
    DEFAULT_WINDOW_DEBOUNCE,
    DEFAULT_WINDOW_MANUAL,
    DOMAIN,
    EVENT_NOTIFICATION,
    WINDOW_CLOSED,
    WINDOW_OPEN,
    WINDOW_POSITION_OPTIONS,
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
# it counts as offline. Long enough that a battery sensor missing a few
# advertisements, or an integration still starting up, does not.
SENSOR_PROBLEM_GRACE = timedelta(minutes=15)
# How often sensor health is re-checked. Silence produces no event to
# react to, so it can only be noticed by looking.
SENSOR_HEALTH_INTERVAL = timedelta(minutes=5)
# After a restart, the states Home Assistant restores all carry a fresh
# last_reported, as if every sensor had just spoken. Reports up to this
# long after startup are not taken as proof of life; the last real one,
# kept in the store, is used instead (see _last_seen).
SENSOR_RESTORE_MARGIN = timedelta(minutes=5)

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
        self.manual: bool = bool(data.get(CONF_WINDOW_MANUAL, DEFAULT_WINDOW_MANUAL))
        # The hand-set position, while manual. Persisted across restarts;
        # dropped in sensor mode, so that switching to manual later starts
        # from what the sensors last saw rather than a stale hand-set value.
        self._manual_position: str | None = None
        # Upper edge of the "sash vertical" band: above it is tilted.
        self._tilt_tilted_above: float = float(
            data.get(CONF_TILT_CLOSED_VALUE, DEFAULT_TILT_CLOSED_VALUE)
        ) + abs(float(data.get(CONF_TILT_TOLERANCE, DEFAULT_TILT_TOLERANCE)))
        self.debounce: int = int(
            data.get(CONF_WINDOW_DEBOUNCE, DEFAULT_WINDOW_DEBOUNCE)
        )
        silence_hours = float(
            data.get(CONF_SENSOR_SILENCE_HOURS, DEFAULT_SENSOR_SILENCE_HOURS)
        )
        self._silence: timedelta | None = (
            timedelta(hours=silence_hours) if silence_hours > 0 else None
        )
        self._notify_persistent: bool = bool(
            data.get(CONF_NOTIFY_PERSISTENT, DEFAULT_NOTIFY_PERSISTENT)
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
        # Sensor health. _problem is the offline sensors already reported
        # (persisted: after a restart it says the event was already fired,
        # so it is not fired again). _problem_shown is whether the repair
        # issue and persistent notification for it exist in THIS session:
        # neither survives a restart, so a restored problem is re-shown.
        self._problem: list[str] = []
        self._problem_shown = False
        # Last real report of each sensor, persisted (see _last_seen).
        self._seen: dict[str, datetime] = {}
        self._missing_since: dict[str, datetime] = {}
        self._trust_reports_after: datetime | None = None
        self._cold_start = False
        # Set by async_started: until then, sensor changes are ignored
        # (see _async_sensor_changed).
        self._started = False
        self._listeners: list[Callable[[], None]] = []
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_health: Callable[[], None] | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.window"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Restore what was known before, then start tracking."""
        await self._async_restore()
        # Set up during startup rather than reloaded later: the sensors'
        # states are about to be restored, not reported (see _last_seen).
        self._cold_start = self.hass.state is not CoreState.running

        if self.manual:
            # Nothing to watch. A problem left over from sensor mode is
            # cleared by the first _check_sensors in async_started.
            return
        if self._sensors:
            self._unsub_state = async_track_state_change_event(
                self.hass, self._sensors, self._async_sensor_changed
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
        if self._cold_start:
            self._trust_reports_after = dt_util.utcnow() + SENSOR_RESTORE_MARGIN
        if not self.manual and self._sensors:
            self._unsub_health = async_track_time_interval(
                self.hass, self._async_health_tick, SENSOR_HEALTH_INTERVAL
            )

        if self.manual and self._manual_position is None:
            # First start in manual mode: carry on from where the sensors
            # last left the window, rather than dropping to unknown.
            if self.position in WINDOW_POSITION_OPTIONS:
                self._manual_position = self.position

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
        if self._unsub_health is not None:
            self._unsub_health()
            self._unsub_health = None

    @property
    def _sensors(self) -> list[str]:
        return [s for s in (self._window_sensor, self._tilt_sensor) if s]

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
        the rotation sensor only distinguishes tilted from a fully open
        sash: tilted is any rotation above the vertical reading plus
        its tolerance. A rotation while the sash reads closed is not a
        state a tilt-and-turn window can physically be in, so it is not treated
        as one.

        In manual mode it is the hand-set position instead.
        """
        if self.manual:
            return self._manual_position or WINDOW_UNKNOWN
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
                    if float(tilt_state.state) > self._tilt_tilted_above:
                        return WINDOW_TILTED
                except (TypeError, ValueError):
                    pass
        return WINDOW_OPEN

    @property
    def is_closed(self) -> bool | None:
        """True/False on the settled position, None while unknown.

        The one question consumers outside the rain logic actually ask:
        a tilted window is open, because air goes through it.
        """
        if self.position is None or self.position == WINDOW_UNKNOWN:
            return None
        return self.position == WINDOW_CLOSED

    @callback
    def set_manual_position(self, position: str) -> None:
        """Set the window position by hand. Manual mode only: the caller
        (the entity's service) refuses it otherwise."""
        if position not in WINDOW_POSITION_OPTIONS:
            raise ValueError(f"not a window position: {position}")
        self._manual_position = position
        if position != self._last_raw:
            self._last_raw = position
            self.raw_since = dt_util.utcnow()
        # No debounce: a position set by hand is not a sensor bouncing.
        self._cancel_pending()
        self._promote(position)
        self._schedule_save()
        self._notify()

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
    # once the sensor has been gone long enough to rule out a hiccup —
    # plus the notifications configured for this cover.
    #
    # "Gone" is either of two things. Unavailable (or missing) for
    # SENSOR_PROBLEM_GRACE. Or silent for sensor_silence_hours: a flat
    # BTHome sensor never turns unavailable, it just stops reporting
    # while Home Assistant keeps showing its last state, so the window
    # looks frozen wherever it was.
    # ------------------------------------------------------------------

    @property
    def offline_sensors(self) -> list[str]:
        """The configured sensors currently reported as offline."""
        return list(self._problem)

    @callback
    def _async_health_tick(self, _now: datetime) -> None:
        self._check_sensors()

    @callback
    def _last_seen(self, entity_id: str, state: State) -> datetime:
        """When the sensor last actually reported.

        state.last_reported, except right after a restart: Home Assistant
        restores every state with a fresh timestamp, which would reset
        the silence count of a sensor that has been dead for days. Those
        are answered from the store, where each real report is recorded.
        """
        reported = state.last_reported
        cutoff = self._trust_reports_after
        if cutoff is None or reported > cutoff:
            if self._seen.get(entity_id) != reported:
                self._seen[entity_id] = reported
                self._schedule_save()
            return reported
        return self._seen.get(entity_id, reported)

    @callback
    def _offline_sensors(self) -> list[str]:
        if self.manual:
            return []
        now = dt_util.utcnow()
        offline = []
        for entity_id in self._sensors:
            state = self.hass.states.get(entity_id)
            if state is None:
                since = self._missing_since.setdefault(entity_id, now)
                if now - since >= SENSOR_PROBLEM_GRACE:
                    offline.append(entity_id)
                continue
            self._missing_since.pop(entity_id, None)
            if state.state in UNAVAILABLE_STATES:
                if now - state.last_changed >= SENSOR_PROBLEM_GRACE:
                    offline.append(entity_id)
                continue
            if (
                self._silence is not None
                and now - self._last_seen(entity_id, state) >= self._silence
            ):
                offline.append(entity_id)
        return offline

    @callback
    def _check_sensors(self) -> None:
        offline = self._offline_sensors()
        if not offline:
            if self._problem:
                self._problem = []
                self._problem_shown = False
                self._clear_problem()
                self._schedule_save()
                self._notify()
            return
        if offline == self._problem and self._problem_shown:
            return
        # Only a problem nobody has been told about yet fires the event.
        # One restored after a restart, or a second sensor of the same
        # window joining the first, only refreshes what HA shows.
        announce = not self._problem
        self._problem = offline
        self._problem_shown = True
        self._show_problem(announce)
        self._schedule_save()
        self._notify()

    @property
    def _issue_id(self) -> str:
        return f"window_sensor_unavailable_{self._entry.entry_id}"

    @property
    def _notification_id(self) -> str:
        """Persistent notification id, and the tag of the phone ones, so
        that clearing the problem can withdraw both."""
        return f"{DOMAIN}_{self._entry.entry_id}_window_sensor"

    @callback
    def _show_problem(self, announce: bool) -> None:
        _LOGGER.warning(
            "%s: window sensor(s) offline (unavailable, or silent for longer "
            "than allowed): %s. The window position may be wrong or unknown.",
            self._name,
            ", ".join(self._problem),
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
                "entities": ", ".join(self._problem),
            },
        )
        self.hass.async_create_task(
            self._async_notify_problem(list(self._problem), announce)
        )

    @callback
    def _clear_problem(self) -> None:
        _LOGGER.info("%s: window sensors are back", self._name)
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        persistent_notification.async_dismiss(self.hass, self._notification_id)
        # Companion-app convention: this message withdraws the
        # notification carrying the same tag instead of showing one.
        self.hass.bus.async_fire(
            EVENT_NOTIFICATION,
            {
                "title": self._name,
                "message": "clear_notification",
                "data": {"tag": self._notification_id},
            },
        )

    async def _async_notify_problem(self, offline: list[str], announce: bool) -> None:
        title, message = await self._async_problem_text(offline)
        if self._notify_persistent:
            persistent_notification.async_create(
                self.hass, message, title=title, notification_id=self._notification_id
            )
        if announce:
            self.hass.bus.async_fire(
                EVENT_NOTIFICATION,
                {
                    "title": title,
                    "message": message,
                    "data": {
                        "tag": self._notification_id,
                        "notification_icon": "mdi:battery-alert-variant-outline",
                    },
                },
            )

    async def _async_problem_text(self, offline: list[str]) -> tuple[str, str]:
        """Title and message, in Home Assistant's language. They live in
        the translations next to the repair issue's own text."""
        strings = await translation.async_get_translations(
            self.hass, self.hass.config.language, "issues", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.issues.window_sensor_notification."
        seen = [
            self._last_seen(entity_id, state)
            for entity_id in offline
            if (state := self.hass.states.get(entity_id)) is not None
        ]
        placeholders = {
            "name": self._name,
            "last_seen": (
                dt_util.as_local(max(seen)).strftime("%d/%m %H:%M") if seen else "—"
            ),
            "battery": self._battery(offline) or "—",
        }
        title = strings.get(prefix + "title", "Window sensor offline: {name}")
        message = strings.get(
            prefix + "description",
            "The window sensor for {name} is not responding "
            "(last heard {last_seen}, battery {battery}).",
        )
        return title.format_map(placeholders), message.format_map(placeholders)

    @callback
    def _battery(self, offline: list[str]) -> str | None:
        """Last battery reading of the offline sensor's device, if it has
        a battery sensor: usually the reason it went quiet."""
        registry = er.async_get(self.hass)
        for entity_id in offline:
            entry = registry.async_get(entity_id)
            if entry is None or entry.device_id is None:
                continue
            for sibling in er.async_entries_for_device(registry, entry.device_id):
                state = self.hass.states.get(sibling.entity_id)
                if (
                    state is not None
                    and state.attributes.get("device_class") == "battery"
                    and state.state not in UNAVAILABLE_STATES
                ):
                    unit = state.attributes.get("unit_of_measurement") or ""
                    return f"{state.state}{unit}"
        return None

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
            "manual_position": self._manual_position,
            "sensor_seen": {
                entity_id: seen.isoformat() for entity_id, seen in self._seen.items()
            },
            "sensor_problem": self._problem,
        }

    async def _async_restore(self) -> None:
        stored = await self._store.async_load()
        if not stored:
            return
        if self.manual:
            self._manual_position = stored.get("manual_position")
        # Only for the sensors configured now: after swapping a sensor in
        # the options, what the old one did says nothing about the new one.
        for entity_id, seen in (stored.get("sensor_seen") or {}).items():
            if entity_id in self._sensors and (parsed := dt_util.parse_datetime(seen)):
                self._seen[entity_id] = parsed
        self._problem = [
            entity_id
            for entity_id in stored.get("sensor_problem") or []
            if entity_id in self._sensors
        ]
        self.position = stored.get("position")
        self.previous = stored.get("previous")
        self.previous_duration = stored.get("previous_duration")
        since = stored.get("since")
        if since:
            self.since = dt_util.parse_datetime(since)
