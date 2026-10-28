"""Smart Cover entity.

Wraps an existing physical cover entity. A regular CoverEntity (domain
`cover`, e.g. `cover.my_shutter`) — this makes it a normal, fully
interoperable cover for anything that already understands the `cover`
domain (Tile card cover-features, voice assistants, generic
automations, ...), while still adding the extra behaviour via custom
services under the `smart_cover` domain: automations can call those to
"just give a hint" — the entity itself decides what to do, based on
its own internal toggles and the current window/rain state.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_start

from .const import (
    CONF_AUTO_RECALIBRATE_ON_RESTART,
    CONF_CLOSED_POSITION_OFFSET,
    CONF_MANUAL_WEATHER_CONDITION,
    CONF_PRESETS,
    CONF_RAIN_POSITION_LIMIT,
    CONF_RESTART_RECALIBRATION_MIN_DELTA,
    CONF_TARGET_COVER,
    CONF_VASISTAS_POSITION_LIMIT,
    CONF_WEATHER_ENTITY,
    DEFAULT_AUTO_RECALIBRATE_ON_RESTART,
    DEFAULT_CLOSED_POSITION_OFFSET,
    DEFAULT_MANUAL_WEATHER_CONDITION,
    DEFAULT_PRESETS,
    DEFAULT_RAIN_POSITION_LIMIT,
    DEFAULT_RAMP_STEPS,
    DEFAULT_RESTART_RECALIBRATION_MIN_DELTA,
    DEFAULT_VASISTAS_POSITION_LIMIT,
    DOMAIN,
    MANUAL_CONDITION_SENSOR,
    MIN_RAMP_STEP_SIZE,
    RAIN_WEATHER_STATES,
    RECALIBRATION_DELAY_SECONDS,
)
from .window import WindowTracker

_LOGGER = logging.getLogger(__name__)


class SmartCoverEntity(CoverEntity, RestoreEntity):
    """"Smart" cover entity wrapping a physical cover.

    Calibration: closed_position_offset stretches this entity's own
    "calibrated"/FINAL 0-100 scale over the part of target_cover's raw
    travel that actually moves the shutter, leaving out the sealing
    stretch at the bottom — see _raw_to_calibrated/_calibrated_to_raw.
    Any position number a caller gives this entity —
    open/close/set_position (so any preset or generic automation
    targeting cover.my_shutter), and rain_position_limit — is a FINAL
    value: 50% is half the shutter's real travel on every cover,
    whatever its offset; the conversion to the physical cover's raw
    scale happens here, once, via _async_set_calibrated_position. The
    one deliberate exception is the recalibration cycle's own travel
    extremes (0/100) and its unrequested restore, which stay RAW —
    exercising the motor's true physical range, and returning to
    exactly where it physically was, has nothing to do with the
    calibration offset. See _async_set_raw_position for that path."""

    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        data: dict[str, Any],
        window: WindowTracker,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._target_cover: str = data[CONF_TARGET_COVER]
        # The window is not read here: WindowTracker (window.py) owns the
        # sensors and the classification, and the window sensor entity
        # publishes what it settles on. This entity only asks it which
        # exposure applies, for the rain caps.
        self._window = window
        self._weather_entity: str | None = data.get(CONF_WEATHER_ENTITY) or None
        self._manual_weather_condition: str = (
            data.get(CONF_MANUAL_WEATHER_CONDITION) or DEFAULT_MANUAL_WEATHER_CONDITION
        )
        self._rain_position_limit: int = int(
            data.get(CONF_RAIN_POSITION_LIMIT, DEFAULT_RAIN_POSITION_LIMIT)
        )
        self._vasistas_position_limit: int = int(
            data.get(CONF_VASISTAS_POSITION_LIMIT, DEFAULT_VASISTAS_POSITION_LIMIT)
        )
        self._closed_position_offset: int = int(
            data.get(CONF_CLOSED_POSITION_OFFSET, DEFAULT_CLOSED_POSITION_OFFSET)
        )
        self._presets: list[int] = list(data.get(CONF_PRESETS) or DEFAULT_PRESETS)
        self._auto_recalibrate_on_restart: bool = bool(
            data.get(
                CONF_AUTO_RECALIBRATE_ON_RESTART, DEFAULT_AUTO_RECALIBRATE_ON_RESTART
            )
        )
        self._restart_recalibration_min_delta: int = int(
            data.get(
                CONF_RESTART_RECALIBRATION_MIN_DELTA,
                DEFAULT_RESTART_RECALIBRATION_MIN_DELTA,
            )
        )

        self._attr_name = data.get("name")
        self._attr_unique_id = f"{DOMAIN}_{self._target_cover}"
        # current_cover_position (below) is CALIBRATED — see
        # _raw_to_calibrated/_calibrated_to_raw. _raw_position is the
        # literal, uncalibrated position reported by target_cover, used
        # for everything that must stay in absolute physical terms
        # regardless of calibration: the rain limits, the
        # recalibration cycle's travel extremes, and restoring the
        # exact pre-cycle position afterwards.
        self._raw_position: int | None = None
        self._attr_current_cover_position: int | None = None
        self._attr_is_closed: bool | None = None
        self._attr_available = False

        # Internal toggles: both on by default. Persisted across restarts
        # via RestoreEntity (see async_added_to_hass).
        self._recalibration_enabled = True
        self._rain_control_enabled = True
        self._recalibration_in_progress = False
        # True only while a MANUAL recalibration (recalibrate_manual) is
        # running — not set for the automatic path (request_recalibration).
        # Exposed as the manual_override_active attribute so an external
        # automation (e.g. one enforcing some unrelated physical safety
        # rule on the same physical cover) can choose to skip itself
        # during this window, by adding its own condition on this
        # entity's state — instead of this integration reaching out to
        # disable that automation directly (fragile, wrong layer).
        self._manual_override_active = False

        # True from startup until the first "long" automatic movement
        # (request_position) is honored, when auto_recalibrate_on_restart
        # is enabled — set for real in async_added_to_hass, which is
        # where hass.state can actually be inspected (see the comment
        # there). Not persisted via RestoreEntity: it exists to catch
        # the first automatic movement of THIS HA session specifically,
        # so it must always be recomputed on add rather than remembered
        # across restarts.
        self._pending_restart_recalibration = False

        # Background ramp task (ramp_to_position). Not persisted across
        # restarts: a ramp interrupted by an HA restart simply stops —
        # the automation that requested it owns the schedule and will
        # call it again next time it's due.
        self._ramp_task: asyncio.Task | None = None
        self._ramp_in_progress: bool = False
        # Identifies the ramp currently in charge. Bumped by every
        # _cancel_ramp(); a ramp step only commands the cover while the
        # generation it started with is still the current one.
        self._ramp_generation = 0

        # Serializes the environmental enforcement pass and the
        # request_position path, which share
        # _pending_requested_position and both command the cover.
        self._enforce_lock = asyncio.Lock()

        # Last position an automation asked for via request_position,
        # while it isn't fully honored yet — because the rain limit in
        # force at the time (or a later environmental change that
        # re-tightened it) is below it. Chased towards, and cleared
        # once fully satisfied, by _async_apply_pending_position (see
        # async_request_position and _async_enforce_rain_limit). Purely
        # in-memory like _recalibration_in_progress/
        # _manual_override_active above — NOT persisted via
        # RestoreEntity: a request lost across a restart isn't
        # remembered, the automation that made it owns re-asking if it
        # still wants it. A manual command (open_cover/close_cover/
        # set_cover_position/close_completely) always clears this —
        # see the precedence rule in those methods below.
        self._pending_requested_position: int | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._attr_name,
            manufacturer="Smart Cover (custom integration)",
            model="Cover wrapper",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "target_cover": self._target_cover,
            "weather_source": (
                self._weather_entity
                if self._manual_weather_condition == MANUAL_CONDITION_SENSOR
                else f"manual:{self._manual_weather_condition}"
            ),
            "recalibration_enabled": self._recalibration_enabled,
            "rain_control_enabled": self._rain_control_enabled,
            "rain_position_limit": self._rain_position_limit,
            "vasistas_position_limit": self._vasistas_position_limit,
            # The cap actually in force right now, or None when nothing
            # is capping the cover (not raining, rain control off, or
            # the window is closed). Saves anyone debugging from having
            # to re-derive it from window_position + is_raining.
            "active_position_limit": self._active_position_limit,
            # True unless the current position exceeds active_position_limit
            # — one combined window+rain+position verdict instead of making
            # every consumer re-derive it from the fields around it.
            "is_safe": self._is_safe,
            # An automation's most recent request_position ask that isn't
            # fully honored yet (still above active_position_limit) — None
            # when there's nothing outstanding. See async_request_position.
            "pending_requested_position": self._pending_requested_position,
            "closed_position_offset": self._closed_position_offset,
            "presets": self._presets,
            "raw_position": self._raw_position,
            # The instantaneous reading this entity acts on for the rain
            # caps, and what the card shows. The SETTLED position — with
            # the previous one and how long it lasted — lives on the
            # window sensor entity (sensor.py), which is what new
            # automations should trigger on.
            "window_position": self._window_position,
            # LEGACY (do not remove). The settled view under its
            # original names, mirrored from the same WindowTracker that
            # feeds the window sensor entity: this entity's state,
            # "previous" and "previous_duration". Automations in this
            # house still trigger on them, and a template reading an
            # attribute that no longer exists fails silently rather than
            # erroring, so removing these breaks triggers with no signal.
            # New consumers should use the sensor entity instead.
            "window_position_stable": self._window_position_stable,
            "window_position_previous": self._window.previous,
            "window_position_previous_duration": (
                None
                if self._window.previous_duration is None
                else round(self._window.previous_duration)
            ),
            "is_raining": self._is_raining,
            "recalibration_in_progress": self._recalibration_in_progress,
            "manual_override_active": self._manual_override_active,
            "ramp_in_progress": self._ramp_in_progress,
            "auto_recalibrate_on_restart": self._auto_recalibrate_on_restart,
            # True until the first long automatic movement after this HA
            # session started has piggybacked a recalibration cycle on
            # itself — see async_added_to_hass/async_request_position.
            # Always False when auto_recalibrate_on_restart is off.
            "pending_restart_recalibration": self._pending_restart_recalibration,
            # Minimum |delta| a request_position ask needs from the
            # current position to count as "long" enough to piggyback
            # the post-restart recalibration cycle — see
            # _should_recalibrate_before.
            "restart_recalibration_min_delta": self._restart_recalibration_min_delta,
        }

    # ------------------------------------------------------------------
    # Setup / lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # A genuine HA restart (power outage included) adds this entity
        # BEFORE hass has finished starting — hass.state is still
        # "starting"/"not_running" here. A config reload (e.g. after
        # editing this cover's options) adds it AFTER HA is already
        # fully up, so hass.state already reads "running" at this same
        # point — same distinction async_at_start's own comment below
        # relies on. Only the former should arm the one-shot
        # auto-recalibration: a mere reload of this config entry isn't
        # evidence the physical motor's position could have drifted.
        self._pending_restart_recalibration = (
            self._auto_recalibrate_on_restart
            and self.hass.state is not CoreState.running
        )

        last_state = await self.async_get_last_state()
        if last_state is not None:
            attrs = last_state.attributes
            if "recalibration_enabled" in attrs:
                self._recalibration_enabled = bool(attrs["recalibration_enabled"])
            if "rain_control_enabled" in attrs:
                self._rain_control_enabled = bool(attrs["rain_control_enabled"])

        self._sync_from_target()

        entities_to_track = [self._target_cover]
        watch_weather = (
            self._manual_weather_condition == MANUAL_CONDITION_SENSOR
            and self._weather_entity
        )
        if watch_weather and self._weather_entity:
            entities_to_track.append(self._weather_entity)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, entities_to_track, self._async_source_changed
            )
        )

        # The window sensors are watched by WindowTracker, not here: it
        # calls back on every change, which is when the exposure this
        # cover is capped to may have changed.
        self.async_on_remove(self._window.add_listener(self._async_window_changed))

        # Startup race condition guard: right after a restart, tracked
        # entities (weather, window/tilt sensors, target_cover) each
        # republish their state independently, in whatever order their
        # own integration happens to be ready — e.g. weather.home is
        # often available before a Bluetooth/BTHome window sensor has
        # had its first advertisement. If the reactive check below ran
        # on the FIRST of those events, _window_position would read
        # "not yet reported" as "unknown" and _rain_limit_for_window
        # would apply the strictest cap (its designed fallback for a
        # window it can't identify), wrongly clamping the position.
        # So: skip the reactive check entirely until HA has fully
        # started, and instead run it exactly once via async_at_start
        # (fires immediately if already running, e.g. on a config
        # reload rather than a full restart) — by then every entity
        # has had its chance to report. It is the same reason YAML
        # automations often wait for a "homeassistant, event: start"
        # trigger.
        self.async_on_remove(async_at_start(self.hass, self._async_handle_started))

    async def _async_handle_started(self, hass: HomeAssistant) -> None:
        await self._async_enforce_rain_limit()

    @callback
    def _async_window_changed(self) -> None:
        """The window moved (or its sensors reported): republish, then
        re-run the environmental check, exactly as for weather."""
        self.async_write_ha_state()
        if self.hass.state is CoreState.running:
            self.hass.async_create_task(self._async_enforce_rain_limit())

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """Single listener for everything this cover tracks (target_cover
        itself, plus window_sensor/tilt_sensor/weather_entity — see
        async_added_to_hass above), but the two kinds of change mean
        different things and must not be treated the same:

        - target_cover changing is just this entity catching up with
          its own position — the result of a manual command (through
          this entity or straight on target_cover, e.g. a Matter
          bridge) or of our own request_position/enforcement writes.
          Resync and republish state, nothing more: running the
          environmental check here too used to mean a manual movement
          got silently re-clamped moments after happening, purely
          because the resulting position change was itself a tracked
          entity. A manual command wins over rain protection — see the
          precedence rule in async_open_cover and siblings.
        - window_sensor/tilt_sensor/weather_entity changing is an
          actual change in the environment this cover reacts to (rain
          started/stopped, exposure worsened/improved): that's what
          should (re-)run the environmental enforcement pass.
        """
        is_target_cover = event.data["entity_id"] == self._target_cover
        if is_target_cover:
            self._sync_from_target()
        self.async_write_ha_state()
        if not is_target_cover and self.hass.state is CoreState.running:
            self.hass.async_create_task(self._async_enforce_rain_limit())

    @callback
    def _sync_from_target(self) -> None:
        state = self.hass.states.get(self._target_cover)
        if state is None or state.state == "unavailable":
            self._attr_available = False
            return
        self._attr_available = True
        position = state.attributes.get(ATTR_CURRENT_POSITION)
        # A target reporting no position leaves ours unknown; _is_safe
        # and the rain clamp both read None as "don't act".
        if position is None:
            self._raw_position = None
            self._attr_current_cover_position = None
        else:
            self._raw_position = int(position)
            self._attr_current_cover_position = self._raw_to_calibrated(
                self._raw_position
            )
        self._attr_is_closed = state.state == "closed"

    # ------------------------------------------------------------------
    # Closed-position calibration
    #
    # The motor's RAW 0-100 travel is not all shutter movement. The
    # bottom slice of it — closed_position_offset wide — is the "seal"
    # stretch: the curtain is already resting on the sill and the motor
    # is only pulling the slats shut. With an offset of 10, raw 0-10 is
    # seal and raw 10 is where the shutter first sits fully down.
    #
    # The CALIBRATED 0-100 scale is what this entity exposes and what
    # every caller speaks: current_cover_position, open/close/
    # set_position, the rain limits, the presets. It spans the shutter's
    # real travel only, stretched over the raw range above the seal:
    #
    #   calibrated   0 -> raw = offset          (fully down)
    #   calibrated 100 -> raw = 100             (fully open)
    #   calibrated  50 -> raw = offset + 45     (halfway, offset 10)
    #
    # The recalibration cycle's travel extremes and its exact-position
    # restore, and close_completely, work in RAW terms and bypass these
    # conversions — see _async_recalibration_cycle and
    # async_close_completely.
    # ------------------------------------------------------------------

    @property
    def _calibrated_span(self) -> int:
        """Width of the raw range the shutter actually travels over."""
        return 100 - self._closed_position_offset

    def _calibrated_to_raw(self, calibrated: int) -> int:
        """Map a calibrated position onto the motor's raw scale.

        The result never lands inside the sealing stretch: the floor is
        the offset itself. Partly-sealed raw positions are physically
        reachable but are not positions this entity offers — sealing is
        all or nothing, through async_close_completely.
        """
        span = self._calibrated_span
        if span <= 0:
            return 100
        raw = self._closed_position_offset + calibrated * span / 100
        return max(self._closed_position_offset, min(100, round(raw)))

    def _raw_to_calibrated(self, raw: int) -> int:
        """Map a raw motor position back onto the calibrated scale.

        Anything inside the seal stretch reads 0%: the shutter is fully
        down there, and only the slats are still moving.
        """
        span = self._calibrated_span
        if span <= 0:
            return 0
        calibrated = (raw - self._closed_position_offset) * 100 / span
        return max(0, min(100, round(calibrated)))

    # ------------------------------------------------------------------
    # Rain limits
    #
    # How far the cover is allowed to be open while it's raining is not
    # a safe/unsafe switch: it depends on how exposed the window is, and
    # there are three levels of that.
    #
    #   window open (casement) -> water gets in -> rain_position_limit,
    #       which keeps the cover almost shut.
    #   window vasistas        -> the gap is up high, so the cover can
    #       sit higher -> vasistas_position_limit.
    #   window closed          -> water never gets in -> no cap.
    #
    # _window_position decides which of the three applies;
    # _active_position_limit turns that into the number in force right
    # now (None = no cap), and every consumer goes through it.
    # ------------------------------------------------------------------

    @property
    def _rain_limit_for_window(self) -> int | None:
        """The cap implied by the window's position alone, ignoring
        whether it's currently raining and whether rain control is on.
        None means "no cap" (window closed).

        Anything we can't positively identify as closed or vasistas
        falls through to the strictest cap: no window_sensor at all, a
        sensor that is unavailable/unknown, or a vasistas we have no
        tilt_sensor to detect. Guessing wrong in the other direction
        would leave the cover open onto a window letting water in.
        """
        position = self._window_position
        if position == "closed":
            return None
        if position == "tilted":
            return self._vasistas_position_limit
        return self._rain_position_limit

    @property
    def _active_position_limit(self) -> int | None:
        """The cap actually in force right now, or None if the cover is
        free to be anywhere: rain control switched off, not raining, or
        a window position that implies no cap."""
        if not self._rain_control_enabled or not self._is_raining:
            return None
        return self._rain_limit_for_window

    @property
    def _window_position(self) -> str:
        """The window's position right now: "closed", "tilted"
        (vasistas), "open" (fully/casement open) or "unknown".

        Instantaneous on purpose, and deliberately NOT the settled
        position the window sensor entity publishes: rain protection has
        to answer the moment a window opens onto the rain, not after the
        debounce. Classification itself lives in WindowTracker, so there
        is one definition of it rather than one per consumer.
        """
        return self._window.raw_position

    @property
    def _window_position_stable(self) -> str | None:
        """The window's SETTLED position, for the legacy attribute of the
        same name (see extra_state_attributes).

        Same value the window sensor entity publishes as its state, read
        from the same tracker rather than re-derived: None while nothing
        has settled yet, so a consumer can tell "not known" from a real
        position instead of matching on the string "unknown".
        """
        position = self._window.position
        if position is None or position == "unknown":
            return None
        return position

    @property
    def _is_raining(self) -> bool:
        """True if it's raining, according to the configured weather
        source.

        manual_weather_condition is checked FIRST: if it's set to the
        special value "sensor", weather_entity is read live (lets you
        pick a weather source per cover, e.g. weather.home/Open-Meteo).
        For any other value, that fixed condition is used directly — a
        value YOU set by hand in the integration's config, checked
        against RAIN_WEATHER_STATES. It only changes when you edit it
        via "Configure" (which reloads the entry). "sensor" without a
        configured weather_entity should not happen (validated in the
        config flow), but defensively falls back to "not raining"."""
        if self._manual_weather_condition == MANUAL_CONDITION_SENSOR:
            if not self._weather_entity:
                return False
            weather_state = self.hass.states.get(self._weather_entity)
            return (
                weather_state is not None
                and weather_state.state in RAIN_WEATHER_STATES
            )
        return self._manual_weather_condition in RAIN_WEATHER_STATES

    @property
    def _limit_active(self) -> bool:
        return self._active_position_limit is not None

    @property
    def _is_safe(self) -> bool:
        """One combined verdict — window position + rain state + the
        cover's own current position — instead of asking every consumer
        to re-derive it from active_position_limit. True whenever
        there's no cap in force, or the current position doesn't
        exceed it (also true if the position isn't known yet)."""
        limit = self._active_position_limit
        position = self._attr_current_cover_position
        return limit is None or position is None or position <= limit

    async def _async_enforce_rain_limit(self) -> None:
        """Serialized wrapper — see _enforce_lock in __init__ for why
        two of these must never run at the same time."""
        async with self._enforce_lock:
            await self._async_enforce_rain_limit_locked()

    async def _async_enforce_rain_limit_locked(self) -> None:
        # The limits are FINAL/calibrated values: "40" always means the
        # same perceived position, regardless of closed_position_offset
        # — the conversion happens here, once, rather than asking every
        # caller to know the offset.
        #
        # Two independent steps, in order:
        #
        # 1. Clamp DOWN if the current position is no longer safe under
        #    the limit now in force (rain just started, or exposure
        #    worsened — window went from vasistas to fully open). This
        #    step never rises the cover on its own just because the
        #    limit relaxed: relaxing on its own would move it with
        #    nobody having asked for that.
        # 2. Chase _pending_requested_position — an automation's most
        #    recent request_position ask that couldn't be (fully)
        #    honored earlier because it was above the limit at the
        #    time. If the limit has since relaxed enough, rise as far
        #    as it now allows, via _async_apply_pending_position (the
        #    same helper async_request_position itself uses, so both
        #    callers agree on how far "as far as allowed" reaches).
        #    This is the only way the cover ever rises on its own: it
        #    is honoring something an automation explicitly asked for,
        #    never inventing a new position.
        #
        # Only ever called for a real environmental change (see
        # _async_source_changed) or once at startup — never because
        # target_cover itself moved, manually or otherwise: a manual
        # command wins over rain protection, and is never corrected
        # after the fact (see async_open_cover and siblings).
        if self._recalibration_in_progress:
            return
        limit = self._active_position_limit
        position = self._attr_current_cover_position
        if limit is not None and position is not None and position > limit:
            _LOGGER.info(
                "%s: raining with window %s, limiting to %s%% (final)",
                self.entity_id,
                self._window_position,
                limit,
            )
            await self._async_set_calibrated_position(limit)
        await self._async_apply_pending_position()

    async def _async_apply_pending_position(self) -> None:
        """Shared by async_request_position (right after storing a new
        pending value) and _async_enforce_rain_limit (after its
        clamp-down step) — one place deciding how far
        _pending_requested_position can be honored under the limit in
        force right now, so the two callers can't drift out of sync.

        Moves towards _pending_requested_position, capped to the
        current limit (None = unclamped, honored in full). Clears
        _pending_requested_position once nothing is left outstanding —
        the limit is None, or the pending value is already within it.
        Otherwise the pending value is left exactly as it was (NOT
        overwritten with the clamped amount): it must keep meaning what
        the automation actually asked for, so a later relaxation can
        still chase the rest of it.

        Guarded by _recalibration_in_progress exactly like the
        clamp-down step above: the motor is busy for an unrelated
        reason, nothing should be commanded from here. A request that
        arrives mid-cycle is still recorded as pending (see
        async_request_position) — it just isn't chased until the next
        environmental trigger or request_position call after the cycle
        ends."""
        if self._recalibration_in_progress:
            return
        pending = self._pending_requested_position
        if pending is None:
            return
        limit = self._active_position_limit
        target = pending if limit is None else min(pending, limit)
        if target != self._attr_current_cover_position:
            await self._async_set_calibrated_position(target)
        if limit is None or pending <= limit:
            self._pending_requested_position = None
        self.async_write_ha_state()

    async def _async_set_raw_position(self, raw_position: int) -> None:
        """Command target_cover directly in its own, uncalibrated scale.

        Forwards this entity's context, which Home Assistant has set to
        the context of whoever called us, so the logbook attributes the
        physical cover's movement to that automation or person.
        """
        await self.hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": self._target_cover, ATTR_POSITION: raw_position},
            blocking=True,
            context=self._context,
        )

    async def _async_set_calibrated_position(self, calibrated_position: int) -> None:
        """Command target_cover in this entity's calibrated scale — what
        a person or a generic automation targeting cover.my_shutter means."""
        await self._async_set_raw_position(
            self._calibrated_to_raw(calibrated_position)
        )

    # ------------------------------------------------------------------
    # Standard cover commands
    #
    # These are user/automation-triggered commands, not the automatic
    # background rain enforcement (_async_enforce_rain_limit). A command
    # coming through here is always considered intentional — a person
    # (or an automation deliberately calling it) is assumed to have
    # already judged the situation — so it is NOT clamped to the
    # rain limits. Toggle rain_control_enabled off if you also
    # want to stop the automatic background enforcement from
    # re-clamping the position afterwards.
    #
    # Precedence rule: a manual command wins over rain protection AND
    # over any outstanding automation request. It is honored in full
    # right now (as above), it clears _pending_requested_position below
    # (an automation's earlier ask must not silently override a manual
    # choice made after it), and — because _async_source_changed no
    # longer re-runs the environmental check for a target_cover change
    # — it is never corrected after the fact either. Automations asking
    # for a position must use the request_position service instead
    # (see async_request_position), which IS clamped and remembered.
    # ------------------------------------------------------------------

    def _begin_manual_command(self) -> None:
        """Enforce the precedence rule above for every manual command:
        cancel any running ramp and drop any outstanding automation
        request, so nothing decided earlier moves the cover afterwards.
        Open, close, set_position, stop and close_completely all start
        here.
        """
        self._cancel_ramp()
        self._pending_requested_position = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        self._begin_manual_command()
        await self._async_set_calibrated_position(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Calibrated close: stops where the cover LOOKS closed (raw =
        closed_position_offset). For the extra travel that seals the
        slats, use the close_completely service instead."""
        self._begin_manual_command()
        await self._async_set_calibrated_position(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        self._begin_manual_command()
        await self._async_set_calibrated_position(int(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        self._begin_manual_command()
        await self.hass.services.async_call(
            "cover",
            "stop_cover",
            {"entity_id": self._target_cover},
            blocking=True,
            context=self._context,
        )

    async def async_close_completely(self) -> None:
        """Always targets true raw 0 — the extra travel below "looks
        closed" that seals the slats together. Ignores
        closed_position_offset entirely, unlike the regular close."""
        self._begin_manual_command()
        _LOGGER.info("%s: closing completely (sealing), raw 0%%", self.entity_id)
        await self._async_set_raw_position(0)

    # ------------------------------------------------------------------
    # Custom services: smart behaviour
    # ------------------------------------------------------------------

    async def _async_recalibration_cycle(
        self, ignore_rain_limit: bool, restore_position: int | None = None
    ) -> None:
        """The travel extremes (0/100) and the restore target are RAW:
        the point is to exercise the motor's true physical travel
        end-to-end (calibration is irrelevant to that). The rain limits
        themselves are FINAL/calibrated values though (see
        _async_enforce_rain_limit), so the clamp check below converts
        between the two rather than comparing raw to calibrated
        directly.

        Where the cycle parks afterwards depends on who asked for it:

        - restore_position None (recalibrate_manual, request_recalibration)
          — nobody asked the cover to be anywhere else, so it returns to
          the exact physical position it was at before the cycle started.
        - restore_position set (async_request_position, which piggybacks
          the post-restart cycle on an automation's ask) — that CALIBRATED
          position is where the cover is wanted, so the cycle ends there
          directly. Returning to the pre-cycle position first would park
          it at a height nobody asked for — typically a stale one, since
          the whole reason this runs is that the session just restarted —
          and cost a full extra traverse to leave again.

        The final step gets the same post-move delay as the close/open
        extremes before recalibration_in_progress drops, because
        set_cover_position's blocking=True only waits for HA to hand the
        command to the physical cover's integration, not for the motor to
        arrive. Without it this method returns while the cover is still
        travelling, and async_request_position's own
        _async_apply_pending_position — which runs right after — compares
        its ask against a position that has not caught up yet, so it fires
        a second set_cover_position over the one still in flight. The
        underlying cover can drop either of the two overlapping commands,
        which is how an automation's ask used to get stranded."""
        self._cancel_ramp()
        self._recalibration_in_progress = True
        self.async_write_ha_state()
        try:
            # Read before the travel below moves it: with no
            # restore_position given, where the cover is right now IS
            # the target to come back to.
            if restore_position is None:
                final_raw_position = self._raw_position or 0
            else:
                final_raw_position = self._calibrated_to_raw(restore_position)
            await self._async_set_raw_position(0)
            await asyncio.sleep(RECALIBRATION_DELAY_SECONDS)
            await self._async_set_raw_position(100)
            await asyncio.sleep(RECALIBRATION_DELAY_SECONDS)
            limit = None if ignore_rain_limit else self._active_position_limit
            if (
                limit is not None
                and self._raw_to_calibrated(final_raw_position) > limit
            ):
                final_raw_position = self._calibrated_to_raw(limit)
            await self._async_set_raw_position(final_raw_position)
            await asyncio.sleep(RECALIBRATION_DELAY_SECONDS)
        finally:
            self._recalibration_in_progress = False
            self.async_write_ha_state()

    async def async_recalibrate_manual(self) -> None:
        """User-forced recalibration: ignores both the
        recalibration_enabled toggle and the rain limits, because
        in this case the user is checking the weather themselves.

        Sets manual_override_active for the duration of the cycle (see
        __init__) — an external automation guarding some unrelated
        physical safety rule on the same cover can add a condition on
        that attribute to skip itself while this is true, instead of
        this integration reaching into that automation directly."""
        # Same reason as in async_request_recalibration: a second,
        # overlapping cycle would restore to a mid-cycle position.
        if self._recalibration_in_progress:
            _LOGGER.info(
                "%s: manual recalibration requested but one is already in progress, ignoring",
                self.entity_id,
            )
            return
        _LOGGER.info("%s: manual recalibration requested by the user", self.entity_id)
        self._manual_override_active = True
        self.async_write_ha_state()
        try:
            await self._async_recalibration_cycle(ignore_rain_limit=True)
        finally:
            self._manual_override_active = False
            self.async_write_ha_state()

    async def async_request_recalibration(self) -> None:
        """Entry point for automations/routines: they just give a hint,
        the entity decides whether to actually proceed.

        A request arriving while a cycle is already running is dropped:
        the motor is being recalibrated right now, so the hint is already
        satisfied. Letting it start a second, overlapping cycle once
        stranded a cover fully open: a post-restart cycle was still
        travelling when a scheduled recalibration arrived a minute later,
        read the cover's mid-cycle position (fully up) as the one to
        restore, and parked it there for the night."""
        if self._recalibration_in_progress:
            _LOGGER.info(
                "%s: recalibration requested but one is already in progress, ignoring",
                self.entity_id,
            )
            return
        if not self._recalibration_enabled:
            _LOGGER.info(
                "%s: recalibration requested but disabled, ignoring",
                self.entity_id,
            )
            return
        if self._limit_active:
            _LOGGER.info(
                "%s: recalibration requested but raining with window %s, ignoring",
                self.entity_id,
                self._window_position,
            )
            return
        await self._async_recalibration_cycle(ignore_rain_limit=False)

    async def async_request_position(self, position: int) -> None:
        """Entry point for automations' position requests — the
        movement counterpart to request_recalibration above, but for
        movement instead of recalibration. NOT the same thing as the
        standard open_cover/close_cover/set_cover_position: those are
        the manual path (a person via the UI/app, or a Matter
        bridge/other controller commanding target_cover directly) —
        honored immediately, never clamped, never remembered, and
        never corrected afterwards (see the precedence rule on those
        methods above). This one is the opposite on all three counts.

        Stores `position` as the most recently requested one —
        replacing whatever was still pending before (last request
        wins; this is NOT "the highest ever requested") — then honors
        as much of it as the rain limit in force right now allows, via
        _async_apply_pending_position (also used by the environmental-
        enforcement path in _async_enforce_rain_limit). A request
        within the current limit — including one that closes further
        than it — is honored in full immediately and leaves nothing
        outstanding. A request above it is only partially honored now,
        and the rest is chased automatically the next time the limit
        relaxes (rain stops, or the window's exposure improves) — no
        second call needed."""
        if self._should_recalibrate_before(position):
            # Cleared synchronously, before the first `await` below, so
            # a second request_position call arriving while the cycle
            # runs can't also see it True and start a second cycle.
            self._pending_restart_recalibration = False
            _LOGGER.info(
                "%s: first long automatic movement after restart, "
                "recalibrating on the way to %s%% (final)",
                self.entity_id,
                position,
            )
            await self._async_recalibration_cycle(
                ignore_rain_limit=False, restore_position=position
            )
        self._cancel_ramp()
        # Storing the request and acting on it is one indivisible step,
        # under the same lock as the environmental pass.
        async with self._enforce_lock:
            self._pending_requested_position = position
            await self._async_apply_pending_position()

    def _should_recalibrate_before(self, requested_position: int) -> bool:
        """Whether this request_position call is the one-shot restart
        recalibration described in async_request_position: still armed
        (pending_restart_recalibration), the feature enabled on this
        cover, recalibration not separately disabled at runtime, and
        the move itself "long" enough (see _restart_recalibration_min_delta,
        the per-cover configurable version of the old fixed threshold)
        that piggybacking the ~60s cycle on it is worth it. An unknown
        current position always counts as long: there is nothing to
        compare a delta against, and it means this is the very first
        position this session has ever set.

        `requested_position` and `current` are both CALIBRATED/FINAL
        percentages (0-100, the "public" scale — see the class
        docstring): the raw scale's extra closed_position_offset
        sealing stretch below it never enters this comparison."""
        if not (
            self._pending_restart_recalibration
            and self._auto_recalibrate_on_restart
            and self._recalibration_enabled
        ):
            return False
        current = self._attr_current_cover_position
        if current is None:
            return True
        return (
            abs(requested_position - current)
            >= self._restart_recalibration_min_delta
        )

    async def async_set_recalibration_enabled(self, enabled: bool) -> None:
        self._recalibration_enabled = enabled
        self.async_write_ha_state()

    async def async_set_rain_control_enabled(self, enabled: bool) -> None:
        self._rain_control_enabled = enabled
        self.async_write_ha_state()
        if enabled:
            await self._async_enforce_rain_limit()

    async def async_activate_rain_protection(self) -> None:
        """Immediately applies the rain limit the window's position
        calls for (a FINAL/calibrated value — see
        _async_enforce_rain_limit), as if it were raining right now.

        Manual override: it skips the rain check and the
        rain_control_enabled toggle, but NOT the window position — with
        the window closed there is still nothing for water to get
        through, so there is nothing to cap."""
        limit = self._rain_limit_for_window
        _LOGGER.info(
            "%s: rain protection activated manually, window %s, limit %s",
            self.entity_id,
            self._window_position,
            limit if limit is not None else "none",
        )
        if limit is None:
            return
        position = self._attr_current_cover_position
        if position is not None and position > limit:
            await self._async_set_calibrated_position(limit)

    # ------------------------------------------------------------------
    # Ramp to position
    #
    # Gradually moves the cover from a start position to a target
    # position, spread across "now" -> end_time in discrete steps.
    # Entry point for automations: they just say "reach X% by this
    # time", the entity owns the pacing and applies the rain limit in
    # force to every step itself (see _rain_capped) — no separate rain
    # logic to keep in sync. Any explicit command
    # (open/close/set_position/stop/recalibration) cancels an
    # in-progress ramp — see _cancel_ramp() and its call sites above.
    # ------------------------------------------------------------------

    def _cancel_ramp(self) -> None:
        """End the running ramp: retire its generation, cancel its task
        and clear the in-progress flag.

        Task.cancel() only takes effect at the task's next await, so a
        step already inside its blocking service call still runs to
        completion; retiring the generation is what stops that step from
        commanding the cover (see _async_ramp_cycle).
        """
        self._ramp_generation += 1
        if self._ramp_task is not None and not self._ramp_task.done():
            self._ramp_task.cancel()
        self._ramp_in_progress = False

    def _rain_capped(self, calibrated_position: int) -> int:
        """Cap a ramp step to the rain limit in force BEFORE commanding
        it.

        Unlike the standard cover commands, a ramp step is not an
        intentional human decision: it is the entity replaying a curve
        decided hours earlier, so it must never open the cover past the
        limit the window's position calls for — not even for the few
        seconds it would take _async_enforce_rain_limit to react (that
        check is reactive: it reads the position reported back by
        target_cover, so it can only pull the cover back AFTER the motor
        has physically run, which is the up-then-down oscillation this
        cap exists to prevent).

        Re-read at every step, so a ramp that starts under an open
        window and continues after it's put on vasistas rises to the
        looser cap on its own. Only opening is capped: a step that is
        MORE CLOSED than the limit is passed through untouched, since
        closing further never lets water in.
        """
        limit = self._active_position_limit
        if limit is None:
            return calibrated_position
        capped = min(calibrated_position, limit)
        if capped != calibrated_position:
            _LOGGER.info(
                "%s: ramp step %s%% capped to %s%% (raining, window %s)",
                self.entity_id,
                calibrated_position,
                capped,
                self._window_position,
            )
        return capped

    async def _async_ramp_cycle(
        self, start: int, end: int, duration_seconds: float, steps: int, generation: int
    ) -> None:
        async def _step(target: int, final: bool = False) -> bool:
            """Command one rain-capped ramp step. Returns False if this
            ramp has been superseded, which tells the caller to stop.

            A non-final step that would move the cover by less than
            MIN_RAMP_STEP_SIZE is skipped (the rain cap or a start
            position next to the current one can produce those). The
            final step only skips a move to where the cover already is,
            so the target is still reached exactly.

            Compared against the last position this ramp commanded, not
            the reported one, which may still lag behind a move just
            sent (start step followed at once by the final one)."""
            nonlocal last
            if generation != self._ramp_generation:
                return False
            capped = self._rain_capped(target)
            if last is not None:
                min_move = 1 if final else MIN_RAMP_STEP_SIZE
                if abs(capped - last) < min_move:
                    _LOGGER.debug(
                        "%s: ramp step to %s%% skipped (cover at %s%%)",
                        self.entity_id,
                        capped,
                        last,
                    )
                    return True
            await self._async_set_calibrated_position(capped)
            last = capped
            return True

        last = self._attr_current_cover_position

        self._ramp_in_progress = True
        self.async_write_ha_state()
        try:
            if not await _step(start):
                return
            if steps <= 0 or duration_seconds <= 0:
                await _step(end, final=True)
                return
            interval = duration_seconds / steps
            for i in range(1, steps + 1):
                await asyncio.sleep(interval)
                progress = i / steps
                if not await _step(
                    round(start + (end - start) * progress), final=i == steps
                ):
                    return
        finally:
            # Only the ramp still holding the current generation owns
            # the flag; a superseded one leaves it to its successor.
            if generation == self._ramp_generation:
                self._ramp_in_progress = False
                self.async_write_ha_state()

    async def async_ramp_to_position(
        self,
        position: int,
        end_time: datetime,
        steps: int = DEFAULT_RAMP_STEPS,
        start_position: int | None = None,
    ) -> None:
        """Entry point for automations: ramps to `position` by
        `end_time`, starting now from `start_position` (defaults to the
        cover's current position). A second call (or any other explicit
        command) supersedes an in-progress ramp — see _cancel_ramp."""
        self._cancel_ramp()
        start = (
            start_position
            if start_position is not None
            else (self._attr_current_cover_position or 0)
        )
        duration_seconds = max(0.0, (end_time - dt_util.utcnow()).total_seconds())
        # Fewer, larger steps when the span is too short for the steps
        # asked: every step then moves at least MIN_RAMP_STEP_SIZE (the
        # rounded positions differ by at least floor(span / steps)).
        steps = max(1, min(steps, abs(position - start) // MIN_RAMP_STEP_SIZE))
        _LOGGER.info(
            "%s: ramping from %s%% to %s%% over %.0fs (%s steps)",
            self.entity_id,
            start,
            position,
            duration_seconds,
            steps,
        )
        # Captured AFTER _cancel_ramp() above bumped it: this ramp owns
        # the current generation, and loses it to whoever cancels next.
        self._ramp_task = self.hass.async_create_task(
            self._async_ramp_cycle(
                start, position, duration_seconds, steps, self._ramp_generation
            )
        )
