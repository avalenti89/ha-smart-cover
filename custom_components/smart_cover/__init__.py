"""Smart Cover integration.

Wraps an existing physical cover entity into a "smart" cover entity
that autonomously decides whether to recalibrate or limit its position
for rain, instead of leaving that logic scattered across automations.
Automations call this entity's custom services (request_recalibration,
activate_rain_protection, ...) and just give a hint: the entity
decides what to do based on its own internal toggles
(recalibration_enabled, rain_control_enabled) and the current
window/rain state.

The entity is a regular CoverEntity (domain `cover`), so it works with
anything that already understands covers — Tile card cover-features,
voice assistants, generic automations — with no extra glue code. Only
the "smart" behaviour is reached through custom services, under the
`smart_cover` domain.

Also registers the custom Lovelace card (frontend/smart-cover-card.js)
as a static resource, so a dashboard can show the cover plus the extra
controls in a single widget, and keeps its dashboard resource pointing
at the current version of the file.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_start

from .const import (
    CONF_TILT_CLOSED_VALUE,
    CONF_TILT_TOLERANCE,
    CONF_TILTED_POSITION_LIMIT,
    DOMAIN,
    FRONTEND_JS_FILENAME,
    FRONTEND_URL_PATH,
    LEGACY_CONF_TILT_THRESHOLD,
    LEGACY_CONF_VASISTAS_POSITION_LIMIT,
)
from .window import WindowTracker

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["cover", "sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await _async_register_frontend(hass)
    return True


def _split_tilt_threshold(values: dict) -> dict:
    """1.1 -> 1.2: "tilted above N degrees" becomes "vertical at 0 degrees,
    tolerance N". Same classification, so no window changes position."""
    if LEGACY_CONF_TILT_THRESHOLD not in values:
        return values
    migrated = dict(values)
    threshold = migrated.pop(LEGACY_CONF_TILT_THRESHOLD)
    migrated.setdefault(CONF_TILT_CLOSED_VALUE, 0)
    migrated.setdefault(CONF_TILT_TOLERANCE, threshold)
    return migrated


def _rename_vasistas_limit(values: dict) -> dict:
    """1.2 -> 1.3: vasistas_position_limit is now tilted_position_limit.
    Same value under the new name."""
    if LEGACY_CONF_VASISTAS_POSITION_LIMIT not in values:
        return values
    migrated = dict(values)
    limit = migrated.pop(LEGACY_CONF_VASISTAS_POSITION_LIMIT)
    migrated.setdefault(CONF_TILTED_POSITION_LIMIT, limit)
    return migrated


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 1:
        # Written by a newer release than this one: don't guess.
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data=_split_tilt_threshold(dict(entry.data)),
            options=_split_tilt_threshold(dict(entry.options)),
            minor_version=2,
        )
        _LOGGER.debug("Smart Cover: migrated %s to 1.2", entry.title)
    if entry.minor_version < 3:
        hass.config_entries.async_update_entry(
            entry,
            data=_rename_vasistas_limit(dict(entry.data)),
            options=_rename_vasistas_limit(dict(entry.options)),
            minor_version=3,
        )
        _LOGGER.debug("Smart Cover: migrated %s to 1.3", entry.title)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    await _async_register_frontend(hass)

    # One window tracker per entry, created before the platforms so both
    # the cover and the window sensor get the same instance: it is the
    # single place that reads the window sensors and classifies them.
    data = {**entry.data, **entry.options}
    tracker = WindowTracker(hass, entry, data)
    await tracker.async_start()
    hass.data[DOMAIN][entry.entry_id] = {"window": tracker}

    # First reading deferred until Home Assistant is up: at setup time a
    # Bluetooth or battery-powered window sensor may not have reported
    # yet, and settling on "unknown" then would record a transition that
    # never happened.
    entry.async_on_unload(async_at_start(hass, _async_started(tracker)))
    entry.async_on_unload(tracker.async_stop)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _async_started(tracker: WindowTracker):
    async def _started(_hass: HomeAssistant) -> None:
        tracker.async_started()

    return _started


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options (sensors/thresholds) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the card's JS file as a static path served by HA core.

    Idempotent: called from both async_setup (no config entry yet) and
    every async_setup_entry, but only registers once.
    """
    if hass.data.get(DOMAIN, {}).get("frontend_registered"):
        return
    frontend_dir = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"{FRONTEND_URL_PATH}/{FRONTEND_JS_FILENAME}",
                str(frontend_dir / FRONTEND_JS_FILENAME),
                cache_headers=False,
            )
        ]
    )
    hass.data.setdefault(DOMAIN, {})["frontend_registered"] = True
    _LOGGER.debug("Smart Cover: frontend card registered at %s", FRONTEND_URL_PATH)
    # Once Home Assistant is up, so that Lovelace has loaded its resources.
    async_at_start(hass, _async_sync_lovelace_resource)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def _async_sync_lovelace_resource(hass: HomeAssistant) -> None:
    """Keep the card's dashboard resource pointing at the current file.

    Browsers cache the card by its URL, so a resource with a fixed
    ?v=... keeps serving the old card after an update until the cache is
    cleared by hand. The URL carries a hash of the file instead, so any
    change to the card changes the URL. The resource is added if it is
    missing. Resources in YAML mode are left alone: only the user's YAML
    can change those.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None or getattr(lovelace, "resource_mode", None) != "storage":
        _LOGGER.debug("Smart Cover: Lovelace resources not in storage mode, skipped")
        return
    path = Path(__file__).parent / "frontend" / FRONTEND_JS_FILENAME
    base = f"{FRONTEND_URL_PATH}/{FRONTEND_JS_FILENAME}"
    try:
        url = f"{base}?v={await hass.async_add_executor_job(_file_digest, path)}"
        resources = lovelace.resources
        # Loads the collection from storage if nobody has asked for it yet.
        await resources.async_get_info()
        for item in resources.async_items():
            if item.get("url", "").split("?")[0] != base:
                continue
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"url": url})
                _LOGGER.info("Smart Cover: card resource updated to %s", url)
            return
        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.info("Smart Cover: card resource added as %s", url)
    except Exception:  # noqa: BLE001 - the card still works, only cached
        _LOGGER.warning(
            "Smart Cover: could not update the card's dashboard resource; "
            "after an update the browser may keep showing the old card",
            exc_info=True,
        )
