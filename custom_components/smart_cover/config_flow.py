"""Config flow for Smart Cover."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    ALL_WEATHER_CONDITIONS,
    CONF_AUTO_RECALIBRATE_ON_RESTART,
    CONF_CLOSED_POSITION_OFFSET,
    CONF_MANUAL_WEATHER_CONDITION,
    CONF_PRESETS,
    CONF_RAIN_POSITION_LIMIT,
    CONF_RESTART_RECALIBRATION_MIN_DELTA,
    CONF_TARGET_COVER,
    CONF_TILT_CLOSED_VALUE,
    CONF_TILT_SENSOR,
    CONF_TILT_TOLERANCE,
    CONF_VASISTAS_POSITION_LIMIT,
    CONF_WEATHER_ENTITY,
    CONF_WINDOW_DEBOUNCE,
    CONF_WINDOW_SENSOR,
    DEFAULT_AUTO_RECALIBRATE_ON_RESTART,
    DEFAULT_CLOSED_POSITION_OFFSET,
    DEFAULT_MANUAL_WEATHER_CONDITION,
    DEFAULT_PRESETS,
    DEFAULT_RAIN_POSITION_LIMIT,
    DEFAULT_RESTART_RECALIBRATION_MIN_DELTA,
    DEFAULT_TILT_CLOSED_VALUE,
    DEFAULT_TILT_TOLERANCE,
    DEFAULT_VASISTAS_POSITION_LIMIT,
    DEFAULT_WINDOW_DEBOUNCE,
    DOMAIN,
    MANUAL_CONDITION_SENSOR,
)

# Collapsible sections of the form (see _schema). Name and target_cover stay
# outside them, always visible; everything else is grouped, so the form opens
# short instead of listing a dozen fields with their long descriptions.
#
# These keys exist ONLY for the form and its translations: Home Assistant
# submits section fields nested under them, and _flatten() puts them back on
# one level before anything is validated or stored. What ends up in the config
# entry therefore keeps exactly the flat shape it has always had — entity.py
# and the already-configured covers are unaffected.
SECTION_WINDOW = "window"
SECTION_RAIN = "rain"
SECTION_POSITIONS = "positions"
SECTIONS = (SECTION_WINDOW, SECTION_RAIN, SECTION_POSITIONS)

# Placeholder text for a field that has no value yet, used by
# _description_placeholders(). Deliberately language-neutral: it is injected
# from here into every translation.
EMPTY_HINT = "—"


def _presets_to_text(presets: list[int]) -> str:
    return ", ".join(str(p) for p in presets)


def _parse_presets(text: str) -> list[int]:
    """Parse a comma-separated list of preset percentages.

    Raises ValueError if any entry isn't an integer 0-100 — caught by
    _validate() below and surfaced as a form error, same pattern as the
    other cross-field checks in this module.
    """
    presets = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        value = int(chunk)
        if not 0 <= value <= 100:
            raise ValueError(f"preset out of range: {value}")
        presets.append(value)
    if not presets:
        raise ValueError("no presets given")
    return presets


def _presets_text(value: Any) -> str:
    """Presets as the comma-separated text the form works in.

    Stored as list[int] once saved, but re-submitted as text — see
    _parse_presets/_presets_to_text.
    """
    if isinstance(value, str):
        return value
    return _presets_to_text(value)


def _hint(value: Any) -> str:
    if value is None or value == "":
        return EMPTY_HINT
    return str(value)


def _description_placeholders(defaults: dict[str, Any] | None = None) -> dict[str, str]:
    """The "currently: X" values quoted in each field's own description.

    Home Assistant passes description_placeholders to the translation of
    every field label and description, section fields included, so each
    field can state the value it currently holds. This is not cosmetic: the
    frontend does not always pre-fill defaults for fields nested inside a
    section, and a field that renders empty would otherwise give no clue
    that a value is already stored — saving the form would then silently
    overwrite it. Written out here, the stored value stays visible either
    way.

    EVERY key referenced by a translation must always be present: a missing
    placeholder makes the frontend drop the whole string, leaving the field
    with no description at all.
    """
    defaults = defaults or {}
    return {
        "current_window_sensor": _hint(defaults.get(CONF_WINDOW_SENSOR)),
        "current_tilt_sensor": _hint(defaults.get(CONF_TILT_SENSOR)),
        "current_tilt_closed_value": _hint(
            defaults.get(CONF_TILT_CLOSED_VALUE, DEFAULT_TILT_CLOSED_VALUE)
        ),
        "current_tilt_tolerance": _hint(
            defaults.get(CONF_TILT_TOLERANCE, DEFAULT_TILT_TOLERANCE)
        ),
        "current_window_debounce": _hint(
            defaults.get(CONF_WINDOW_DEBOUNCE, DEFAULT_WINDOW_DEBOUNCE)
        ),
        "current_weather_entity": _hint(defaults.get(CONF_WEATHER_ENTITY)),
        "current_manual_weather_condition": _hint(
            defaults.get(
                CONF_MANUAL_WEATHER_CONDITION, DEFAULT_MANUAL_WEATHER_CONDITION
            )
        ),
        "current_rain_position_limit": _hint(
            defaults.get(CONF_RAIN_POSITION_LIMIT, DEFAULT_RAIN_POSITION_LIMIT)
        ),
        "current_vasistas_position_limit": _hint(
            defaults.get(
                CONF_VASISTAS_POSITION_LIMIT, DEFAULT_VASISTAS_POSITION_LIMIT
            )
        ),
        "current_closed_position_offset": _hint(
            defaults.get(CONF_CLOSED_POSITION_OFFSET, DEFAULT_CLOSED_POSITION_OFFSET)
        ),
        "current_presets": _hint(
            _presets_text(defaults.get(CONF_PRESETS, DEFAULT_PRESETS))
        ),
        "current_auto_recalibrate_on_restart": _hint(
            defaults.get(
                CONF_AUTO_RECALIBRATE_ON_RESTART, DEFAULT_AUTO_RECALIBRATE_ON_RESTART
            )
        ),
        "current_restart_recalibration_min_delta": _hint(
            defaults.get(
                CONF_RESTART_RECALIBRATION_MIN_DELTA,
                DEFAULT_RESTART_RECALIBRATION_MIN_DELTA,
            )
        ),
    }


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}

    # "sensor" (read weather_entity instead of a fixed condition) is only
    # offered as a choice once weather_entity has a value — Home Assistant
    # selectors can't grey out a single option live based on a sibling
    # field, so this is evaluated from the current form defaults each time
    # the form is (re)shown; see the cross-field check in the flow steps
    # below for the case where both change in the same submission.
    condition_options = list(ALL_WEATHER_CONDITIONS)
    if defaults.get(CONF_WEATHER_ENTITY):
        condition_options = [MANUAL_CONDITION_SENSOR] + condition_options

    condition_default = defaults.get(
        CONF_MANUAL_WEATHER_CONDITION, DEFAULT_MANUAL_WEATHER_CONDITION
    )
    if condition_default not in condition_options:
        # Stored value was "sensor" but weather_entity is now empty (or
        # this is a fresh form): fall back to a valid, safe default.
        condition_default = DEFAULT_MANUAL_WEATHER_CONDITION

    window_fields: dict[Any, Any] = {
        vol.Optional(
            CONF_WINDOW_SENSOR, default=defaults.get(CONF_WINDOW_SENSOR, "")
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="binary_sensor")
        ),
        vol.Optional(
            CONF_TILT_SENSOR, default=defaults.get(CONF_TILT_SENSOR, "")
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_TILT_CLOSED_VALUE,
            default=defaults.get(CONF_TILT_CLOSED_VALUE, DEFAULT_TILT_CLOSED_VALUE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-180,
                max=180,
                step=0.1,
                unit_of_measurement="°",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_TILT_TOLERANCE,
            default=defaults.get(CONF_TILT_TOLERANCE, DEFAULT_TILT_TOLERANCE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=90,
                step=0.1,
                unit_of_measurement="°",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_WINDOW_DEBOUNCE,
            default=defaults.get(CONF_WINDOW_DEBOUNCE, DEFAULT_WINDOW_DEBOUNCE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=300,
                step=1,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }

    rain_fields: dict[Any, Any] = {
        vol.Optional(
            CONF_WEATHER_ENTITY, default=defaults.get(CONF_WEATHER_ENTITY, "")
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        vol.Optional(
            CONF_MANUAL_WEATHER_CONDITION, default=condition_default
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=condition_options,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(
            CONF_RAIN_POSITION_LIMIT,
            default=defaults.get(
                CONF_RAIN_POSITION_LIMIT, DEFAULT_RAIN_POSITION_LIMIT
            ),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, mode=selector.NumberSelectorMode.SLIDER
            )
        ),
    }

    # The vasistas cap is only offered when a tilt_sensor is set: with no
    # rotation reading _window_position can never return "tilted", so the
    # cap would be unreachable and the field would be a lie. Evaluated
    # from the current form defaults each time the form is (re)shown,
    # same approach as condition_options above.
    if defaults.get(CONF_TILT_SENSOR):
        rain_fields[
            vol.Optional(
                CONF_VASISTAS_POSITION_LIMIT,
                default=defaults.get(
                    CONF_VASISTAS_POSITION_LIMIT, DEFAULT_VASISTAS_POSITION_LIMIT
                ),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, mode=selector.NumberSelectorMode.SLIDER
            )
        )

    position_fields: dict[Any, Any] = {
        vol.Optional(
            CONF_CLOSED_POSITION_OFFSET,
            default=defaults.get(
                CONF_CLOSED_POSITION_OFFSET, DEFAULT_CLOSED_POSITION_OFFSET
            ),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=50, mode=selector.NumberSelectorMode.SLIDER
            )
        ),
        vol.Optional(
            CONF_PRESETS,
            default=_presets_text(defaults.get(CONF_PRESETS, DEFAULT_PRESETS)),
        ): selector.TextSelector(),
        vol.Optional(
            CONF_AUTO_RECALIBRATE_ON_RESTART,
            default=defaults.get(
                CONF_AUTO_RECALIBRATE_ON_RESTART, DEFAULT_AUTO_RECALIBRATE_ON_RESTART
            ),
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_RESTART_RECALIBRATION_MIN_DELTA,
            default=defaults.get(
                CONF_RESTART_RECALIBRATION_MIN_DELTA,
                DEFAULT_RESTART_RECALIBRATION_MIN_DELTA,
            ),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, mode=selector.NumberSelectorMode.SLIDER
            )
        ),
    }

    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
            vol.Required(
                CONF_TARGET_COVER, default=defaults.get(CONF_TARGET_COVER)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="cover")
            ),
            vol.Required(SECTION_WINDOW): section(
                vol.Schema(window_fields), {"collapsed": False}
            ),
            vol.Required(SECTION_RAIN): section(
                vol.Schema(rain_fields), {"collapsed": True}
            ),
            vol.Required(SECTION_POSITIONS): section(
                vol.Schema(position_fields), {"collapsed": True}
            ),
        }
    )


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Collapse the per-section sub-dicts back onto one level.

    Home Assistant returns section fields nested under their section key;
    the config entry keeps the flat shape every existing cover already has,
    so the nesting stops here and never reaches storage or entity.py.
    """
    flat = {key: value for key, value in user_input.items() if key not in SECTIONS}
    for key in SECTIONS:
        nested = user_input.get(key)
        if isinstance(nested, dict):
            flat.update(nested)
    return flat


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """Cross-field checks, run on the already-flattened input.

    "sensor" requires weather_entity to be set — belt-and-suspenders for
    the case where a caller submits both in the same request (e.g. a
    script, or a race in the UI) bypassing the options list computed by
    _schema().

    Both checks report under "base" instead of on the offending field:
    those fields live inside collapsible sections, and a field-level error
    would be invisible while its section is collapsed, leaving a form that
    refuses to save with no message anywhere.
    """
    errors: dict[str, str] = {}
    if (
        user_input.get(CONF_MANUAL_WEATHER_CONDITION) == MANUAL_CONDITION_SENSOR
        and not user_input.get(CONF_WEATHER_ENTITY)
    ):
        errors["base"] = "sensor_requires_weather_entity"

    presets = user_input.get(CONF_PRESETS)
    if "base" not in errors and isinstance(presets, str):
        try:
            _parse_presets(presets)
        except ValueError:
            errors["base"] = "invalid_presets"

    return errors


class SmartCoverConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow: one instance = one physical cover."""

    VERSION = 1
    # 2: tilt_threshold split into tilt_closed_value + tilt_tolerance
    # (see async_migrate_entry in __init__.py).
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        current: dict[str, Any] = {}
        if user_input is not None:
            current = _flatten(user_input)
            errors = _validate(current)
            if not errors:
                await self.async_set_unique_id(current[CONF_TARGET_COVER])
                self._abort_if_unique_id_configured()
                current[CONF_PRESETS] = _parse_presets(current[CONF_PRESETS])
                return self.async_create_entry(
                    title=current[CONF_NAME], data=current
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(current),
            errors=errors,
            description_placeholders=_description_placeholders(current),
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return SmartCoverOptionsFlow(config_entry)


class SmartCoverOptionsFlow(config_entries.OptionsFlow):
    """Lets the user edit sensors/thresholds after creation."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        submitted: dict[str, Any] = {}
        if user_input is not None:
            submitted = _flatten(user_input)
            errors = _validate(submitted)
            if not errors:
                submitted[CONF_PRESETS] = _parse_presets(submitted[CONF_PRESETS])
                return self.async_create_entry(title="", data=submitted)

        current = {**self._entry.data, **self._entry.options, **submitted}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(current),
            errors=errors,
            description_placeholders=_description_placeholders(current),
        )
