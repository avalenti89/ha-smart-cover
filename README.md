# Smart Cover

A Home Assistant custom integration that wraps an existing `cover.*` entity
into a new `cover.*` entity of its own, with rain limits and recalibration
logic built in — instead of scattering that logic across several
automations. It's a regular cover (works with Tile cards, voice
assistants, generic automations, ...); the extra behaviour is reached
through custom services under the `smart_cover` domain.

## Why

A common pattern for outdoor covers/shutters is:

- Never let the cover open more than the local building code allows an
  awning to when it's raining and the window is open.
- Periodically recalibrate a cover's physical end-stops.
- Let automations *request* these behaviours without each of them having
  to re-implement the same rain/window safety checks.

Smart Cover moves that logic into the entity itself. Automations just call
a service to give a hint (`request_recalibration`, ...); the entity decides
whether to actually act, based on its own toggles and the current
rain/window state.

## Features

- Wraps any existing `cover.*` entity — the physical entity is untouched
  and keeps working with any automation, voice assistant, or dashboard
  that already targets it.
- Rain position limits: while it's raining (from a `weather.*` entity or
  a fixed condition), how far the cover may be open depends on how
  exposed the associated window is. Three levels, two of them
  configurable:
  - window fully open (casement) — water gets in, so the cover is kept
    almost shut (`rain_position_limit`, default 10%);
  - window at vasistas (tilted past the configurable vertical reading + tolerance) — the gap
    is up high, so the cover may sit higher (`vasistas_position_limit`,
    default 40%);
  - window closed — water never gets in, no limit at all.

  A window whose position can't be determined (no sensor, sensor
  unavailable, or a vasistas with no tilt sensor to detect it) gets the
  strictest limit. The clamp only ever closes: when the limit relaxes
  the cover stays where it is rather than rising to the new limit.
- Manual recalibration (`recalibrate_manual`) and automation-driven
  recalibration (`request_recalibration`) — the latter is skipped
  automatically when disabled or when it's unsafe to move the cover.
  All manual movement commands (`open`, `close`, `set_position`,
  `recalibrate_manual`) skip this integration's own rain clamp —
  a manual call is treated as a deliberate, already-judged action.
- A `manual_override_active` attribute, true only while a manual
  recalibration (`recalibrate_manual`) is running. If your cover is
  also guarded by some other, unrelated automation (e.g. "never fully
  close while a heater is on"), have IT check this attribute and skip
  itself while it's true — rather than this integration reaching into
  that automation directly, which would be the wrong layer for it.
- Closed-position calibration (`closed_position_offset`): for covers
  whose motor has extra travel below where the slats *look* closed
  (travel that seals them shut, with no further visible movement),
  this entity's own 0-100% scale can be shifted so its 0% lines up
  with "looks closed" instead of the physical end of travel. Off by
  default (0 = no calibration, identical to the wrapped cover's own
  scale). Every position number given to this entity — open/close/
  set_position (so any preset or generic automation targeting
  cover.my_shutter), and `rain_position_limit` — is a FINAL value: "30"
  always means the same perceived position, whatever the offset is;
  the conversion to the physical cover's own raw scale happens
  automatically, in one place. The one deliberate exception is the
  recalibration cycle's own travel extremes and its exact-position
  restore, which stay in the physical cover's raw terms — exercising
  the motor's true range has nothing to do with this calibration. Use
  the `close_completely` service to reach the true physical 0%
  (sealed) regardless of the offset.
- Runtime toggles (`set_recalibration_enabled`,
  `set_rain_control_enabled`) so you can disable either behaviour from an
  automation or a dashboard, no need to edit YAML.
- Auto-recalibration after a Home Assistant restart
  (`auto_recalibrate_on_restart`, on by default): after a genuine restart
  (power outage included — anything that isn't just this config entry
  being reloaded), the first time an automation's `request_position` asks
  for a wide move, a full recalibration cycle runs first. Catches a motor
  that may have drifted out of sync with HA's remembered position while it
  was down. Only `request_position` can trigger it — manual commands
  never do — and it still respects `recalibration_enabled`.
- A custom Lovelace card (`custom:smart-cover-card`) showing position
  slider, rain/window status, the two toggles, and the two manual
  actions in one widget. This is the only UI for the extra controls —
  Home Assistant's "more info" popup can't be extended to a custom
  domain (it's hardcoded per-domain for a fixed set of built-ins like
  `climate`/`cover`), so clicking the entity elsewhere in the UI shows
  the plain default popup instead.

## Installation

### HACS (recommended once published)

1. HACS → Integrations → ⋮ → Custom repositories → add this repo's URL,
   category "Integration".
2. Install "Smart Cover", restart Home Assistant.

### Manual

1. Copy `custom_components/smart_cover` into your Home Assistant's
   `custom_components/` directory.
2. Restart Home Assistant.

## Configuration

Settings → Devices & Services → Add Integration → "Smart Cover". One
instance = one physical cover.

| Field | Required | Description |
|---|---|---|
| Name | yes | Display name for this smart cover. |
| Physical cover to wrap | yes | The existing `cover.*` entity to control. |
| Window sensor | no | `binary_sensor.*` for the room's window (open/closed). Decides which rain limit applies. If left empty, the strictest one always applies. |
| Tilt sensor | no | Numeric rotation sensor for the same window, to detect a tilted/vasistas opening. Without it vasistas can't be told apart from a fully open window, and the stricter limit applies. |
| Tilt with the sash vertical | no | What the tilt sensor reads with the sash vertical (shut, or open as a casement). Default: 0. |
| Vertical tilt tolerance | no | ± degrees a reading may stray from the vertical value and still count as vertical. Above vertical + tolerance the window is tilted; anything else counts as fully open. Default: 0.2. Entries created before 0.5.0 are migrated to vertical 0 with their old threshold as tolerance, so they classify exactly as before. |
| Weather entity | no | `weather.*` entity to read for rain detection. Only used when "Manual weather condition" below is set to `sensor`. |
| Manual weather condition | no | Either a fixed weather condition (sunny, rainy, cloudy, ...) set by hand, or the special value `sensor` to read "Weather entity" above live instead. `sensor` is only offered as a choice once a weather entity is set. A fixed condition only changes when you edit it here via "Configure". Default: `sunny`. |
| Rain position limit | no | Maximum FINAL/perceived opening (%) allowed while it's raining and the window is fully open, or its position is unknown — see calibration note above. Default: 10. |
| Vasistas position limit | no | Maximum FINAL/perceived opening (%) allowed while it's raining and the window is at vasistas. Only offered when a tilt sensor is set. Default: 40. |
| Closed-position offset | no | Extra sealing travel (%) below where the cover looks closed. Shifts this entity's own 0-100% scale — see Features above. Default: 0. |
| Preset percentages | no | Comma-separated quick-access position buttons shown on the dashboard card, e.g. `0, 25, 50, 75, 100`. Used unless the card itself sets its own `presets:` (see Dashboard card below). Default: `0, 25, 50, 75, 100`. |
| Auto-recalibrate after a restart | no | After a genuine HA restart, run a full recalibration cycle on the first "long" automatic move (`request_position`) — see Features above. Default: on. |

All fields can be changed later via the integration's "Configure" button.

## Services

Standard movement uses the native `cover` domain (`cover.open_cover`,
`cover.close_cover`, `cover.set_cover_position`, `cover.stop_cover`) —
nothing custom there. The extra behaviour is under the `smart_cover`
domain; see `services.yaml` / the Developer Tools → Actions picker for
the full list: `smart_cover.close_completely`,
`smart_cover.recalibrate_manual`,
`request_recalibration`, `set_recalibration_enabled`,
`set_rain_control_enabled`, `activate_rain_protection`.

## Dashboard card

```yaml
type: custom:smart-cover-card
entity: cover.my_shutter
presets: [0, 25, 50, 75, 100]   # optional, % position buttons — overrides
                                # the integration's own "Preset percentages"
                                # setting above if given
```

Shows: a header (click it to open the entity's native cover "more info"
popup — history, and Home Assistant's own open/close/position UI), a stop
button + slider + numeric input (type a percentage directly), a "Seal"
button (`close_completely`) followed by the position preset buttons,
rain/window status, and the two toggles + two manual actions this
integration adds. This card is the only way to reach those last three —
Home Assistant's built-in popups can't be extended with custom rows.

## License

MIT — see `LICENSE`.
