/**
 * Frontend for the Smart Cover integration.
 *
 * Defines <smart-cover-card>, a regular Lovelace card
 * (type: custom:smart-cover-card) you place on a dashboard. It shows: cover
 * position (via preset buttons + slider + stop, over the native `cover`
 * domain), rain/window status, and the two extra toggles + two extra
 * actions this integration adds (via the smart_cover.* custom services) —
 * all in one widget, no separate switch/button entities involved.
 *
 * Note: the entity is a regular cover.* entity, so Home Assistant's native
 * cover "more info" popup (open/close/position/history) already exists for
 * it — that part is NOT custom. This card adds a shortcut to open it (the
 * name/position header), since the popup itself can't show this
 * integration's extra recalibration/rain-control controls (Home Assistant's
 * built-in popups can't be extended with custom rows) — this card is the
 * only way to reach those.
 *
 * Config:
 *   type: custom:smart-cover-card
 *   entity: cover.my_shutter
 *   presets: [0, 25, 50, 75, 100]   # optional, % positions shown as buttons
 *                                   # — overrides the entity's own "presets"
 *                                   # attribute (set via the integration's
 *                                   # options) if given; otherwise that
 *                                   # attribute is used, falling back to
 *                                   # DEFAULT_PRESETS below if neither is set.
 *   hide_slider: true               # optional, default false — hides the
 *                                   # drag slider (too easy to bump
 *                                   # accidentally on a touchscreen); the
 *                                   # number input, preset buttons and stop
 *                                   # button still work. Typically paired
 *                                   # with a `screen` visibility condition
 *                                   # on the card's section so only the
 *                                   # mobile-width copy of the card sets it.
 */

const DOMAIN = "smart_cover";
const DEFAULT_PRESETS = [0, 25, 50, 75, 100];

// Explicit window positions, as reported by the entity's window_position
// attribute. Each maps to a different rain cap on the cover (see
// entity.py): "open" (casement) is the most exposed, "tilted" — the
// vasistas/top-hung opening — gets a looser cap because the gap is up
// high, and "closed" gets none at all.
const WINDOW_LABELS = {
  closed: "Window closed",
  tilted: "Window vasistas",
  open: "Window open",
  unknown: "Window unknown",
};

const STYLE = `
  .sc-root { padding: 16px; }
  .sc-root.sc-in-card { padding: 16px; }
  .sc-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 8px;
    cursor: pointer;
    border-radius: 8px;
    padding: 4px;
    margin: -4px -4px 4px -4px;
  }
  .sc-header:hover { background: var(--secondary-background-color); }
  .sc-name {
    font-size: 1.2em;
    font-weight: 500;
    color: var(--primary-text-color);
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .sc-name ha-icon {
    --mdc-icon-size: 18px;
    color: var(--secondary-text-color);
  }
  .sc-position {
    font-size: 1em;
    color: var(--secondary-text-color);
  }
  .sc-cover-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  /* pan-y leaves vertical swipes to the page, so only a horizontal
     drag reaches the slider. */
  .sc-slider { flex: 1; touch-action: pan-y; }
  .sc-number-input {
    width: 3.5em;
    border: 1px solid var(--divider-color);
    border-radius: 6px;
    padding: 4px 2px;
    background: var(--card-background-color, var(--primary-background-color));
    color: var(--primary-text-color);
    font-size: 0.9em;
    text-align: center;
  }
  .sc-preset-row {
    display: flex;
    gap: 6px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }
  .sc-preset {
    flex: 1;
    min-width: 40px;
    border: none;
    border-radius: 8px;
    padding: 8px 4px;
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
    font-size: 0.85em;
    font-weight: 500;
    cursor: pointer;
  }
  .sc-preset:hover { filter: brightness(1.1); }
  .sc-preset:active { filter: brightness(0.9); }
  .sc-preset.active {
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  .sc-close-completely {
    flex: 1.4;
    min-width: 56px;
    border: 2px solid var(--primary-color);
    border-radius: 8px;
    padding: 8px 4px;
    background: transparent;
    color: var(--primary-color);
    font-size: 0.85em;
    font-weight: 500;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
  }
  .sc-close-completely ha-icon { --mdc-icon-size: 16px; }
  .sc-close-completely:hover { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .sc-status {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    font-size: 0.85em;
    color: var(--secondary-text-color);
    margin-bottom: 8px;
  }
  .sc-status-item.active {
    color: var(--warning-color, #ff9800);
    font-weight: 500;
  }
  .sc-divider { border-top: 1px solid var(--divider-color); margin: 8px 0; }
  .sc-toggle-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 0;
  }
  .sc-toggle-label { color: var(--primary-text-color); }
  .sc-action-row {
    display: flex;
    gap: 8px;
    margin-top: 12px;
    flex-wrap: wrap;
  }
  .sc-btn {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-width: 140px;
    border: none;
    border-radius: 8px;
    padding: 10px 12px;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
    font-size: 0.9em;
    font-weight: 500;
    cursor: pointer;
  }
  .sc-btn ha-icon { --mdc-icon-size: 18px; }
  .sc-btn:hover { filter: brightness(1.1); }
  .sc-btn:active { filter: brightness(0.9); }
  .sc-btn[disabled] {
    opacity: 0.5;
    cursor: default;
    filter: none;
  }
  .sc-error { padding: 16px; color: var(--error-color, red); }
  .sc-unavailable { opacity: 0.5; pointer-events: none; }
  .sc-hide-slider .sc-slider { display: none; }
  .sc-hide-slider .sc-number-input { flex: 1; width: 100%; }
`;

function contentHtml(presets) {
  const presetButtons = presets
    .map((p) => `<button class="sc-preset" data-position="${p}">${p}%</button>`)
    .join("");

  return `
    <div class="sc-header">
      <div class="sc-name"><span class="sc-name-text"></span><ha-icon icon="mdi:open-in-new"></ha-icon></div>
      <div class="sc-position"></div>
    </div>

    <div class="sc-cover-row">
      <ha-icon-button class="sc-stop" label="Stop">
        <ha-icon icon="mdi:stop"></ha-icon>
      </ha-icon-button>
      <!-- value="0" is explicit because a range input with no value
           attribute starts at the midpoint of its range, not at min. -->
      <input type="range" min="0" max="100" value="0" class="sc-slider" />
      <input type="number" min="0" max="100" class="sc-number-input" />
    </div>

    <div class="sc-preset-row">
      <button class="sc-close-completely" title="Close completely (seal the slats) — bypasses the closed-position offset">
        <ha-icon icon="mdi:lock-outline"></ha-icon>Seal
      </button>
      ${presetButtons}
    </div>

    <div class="sc-status">
      <span class="sc-status-item sc-rain"></span>
      <span class="sc-status-item sc-window"></span>
      <span class="sc-status-item sc-recal-running"></span>
    </div>

    <div class="sc-divider"></div>

    <div class="sc-toggle-row">
      <span class="sc-toggle-label">Automatic recalibration</span>
      <ha-switch class="sc-toggle-recal"></ha-switch>
    </div>
    <div class="sc-toggle-row">
      <span class="sc-toggle-label">Rain control</span>
      <ha-switch class="sc-toggle-rain"></ha-switch>
    </div>

    <div class="sc-action-row">
      <button class="sc-btn sc-btn-recal-manual">
        <ha-icon icon="mdi:autorenew"></ha-icon>
        Recalibrate now
      </button>
      <button class="sc-btn sc-btn-rain-protect">
        <ha-icon icon="mdi:weather-pouring"></ha-icon>
        Rain protection now
      </button>
    </div>
  `;
}

/**
 * Base class: builds the DOM once, wires up events once, and updates it
 * whenever hass/entityId change. There is currently only one subclass
 * (SmartCoverCard) — kept separate in case another entry point (e.g. a
 * dashboard strategy) needs the same rendering later.
 */
class SmartCoverElementBase extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._entityId = null;
    // Card config always wins when given; otherwise falls back to the
    // entity's own "presets" attribute (set via the integration's
    // options — see config_flow.py), and only then to the hardcoded
    // default. null means "not set by the card config".
    this._configPresets = null;
    this._presets = DEFAULT_PRESETS;
    // Set from config.hide_slider (SmartCoverCard.setConfig) before the
    // first _build(); hides the drag slider while leaving the number
    // input, presets and stop button in place. See class doc comment.
    this._hideSlider = false;
    this._built = false;
    // True between a pointerdown on the slider and the change event it
    // produces.
    this._sliderTouched = false;
    // True while _update() is writing control state from the entity.
    this._syncing = false;
  }

  // Overridden to true by SmartCoverCard to get an <ha-card> wrapper.
  get _wrapInCard() {
    return false;
  }

  // Custom smart_cover.* services (recalibration, toggles, ...).
  _callService(service, data) {
    this._hass.callService(DOMAIN, service, {
      entity_id: this._entityId,
      ...data,
    });
  }

  // Standard movement — the entity is a regular cover.* entity, so
  // these go through the native `cover` domain, the same as any other
  // cover card/automation would call.
  _callCoverService(service, data) {
    this._hass.callService("cover", service, {
      entity_id: this._entityId,
      ...data,
    });
  }

  // Single place that turns a raw input value into a position command:
  // both the slider and the number box go through it, so they can't
  // drift apart on clamping or on what counts as a valid number.
  // Returns the value actually sent, or null if there was nothing valid
  // to send.
  _commitPosition(rawValue) {
    const parsed = parseInt(rawValue, 10);
    if (Number.isNaN(parsed)) {
      return null;
    }
    const position = Math.max(0, Math.min(100, parsed));
    this._callCoverService("set_cover_position", { position });
    return position;
  }

  // Writes a value into a control without fighting the user: skipped
  // while that control has focus (they are mid-edit) and while the value
  // already matches, so no control is touched more often than it needs
  // to be.
  _syncControl(selector, value) {
    const el = this.shadowRoot.querySelector(selector);
    // el.value is always a string; compare as one so an unchanged
    // position doesn't count as a difference and rewrite the control.
    if (this.shadowRoot.activeElement === el || el.value === String(value)) {
      return;
    }
    el.value = value;
  }

  // Same idea for the <ha-switch>es, but guarded by _syncing: see the
  // change handlers in _build() for why a programmatic write must not be
  // mistaken for a user toggling the switch.
  _syncToggle(selector, checked) {
    const el = this.shadowRoot.querySelector(selector);
    if (el.checked === checked) {
      return;
    }
    this._syncing = true;
    try {
      el.checked = checked;
    } finally {
      this._syncing = false;
    }
  }

  // Opens Home Assistant's native "more info" dialog for this entity —
  // the same dialog you'd get clicking the entity anywhere else in the
  // UI (position/open/close/history). Standard HA event, bubbles up
  // through the shadow boundary (composed: true) to wherever the
  // dialog manager is listening.
  _openMoreInfo() {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId: this._entityId },
        bubbles: true,
        composed: true,
      })
    );
  }

  _renderError(message) {
    this.shadowRoot.innerHTML = `<div class="sc-error">${message}</div><style>${STYLE}</style>`;
    this._built = false;
  }

  _build() {
    const inner = this._wrapInCard
      ? `<ha-card><div class="sc-root sc-in-card">${contentHtml(this._presets)}</div></ha-card>`
      : `<div class="sc-root">${contentHtml(this._presets)}</div>`;
    this.shadowRoot.innerHTML = `${inner}<style>${STYLE}</style>`;
    // The controls these flags described have just been replaced.
    this._sliderTouched = false;
    this._syncing = false;

    const root = this.shadowRoot;
    root.querySelector(".sc-root").classList.toggle("sc-hide-slider", this._hideSlider);
    root.querySelector(".sc-header").addEventListener("click", () => this._openMoreInfo());
    root.querySelector(".sc-stop").addEventListener("click", () =>
      this._callCoverService("stop_cover", {})
    );
    // The slider commands the cover only for a change that follows a
    // pointerdown on it, i.e. one the user produced themselves.
    const slider = root.querySelector(".sc-slider");
    slider.addEventListener("pointerdown", () => {
      this._sliderTouched = true;
    });
    slider.addEventListener("change", (ev) => {
      if (!this._sliderTouched) {
        return;
      }
      this._sliderTouched = false;
      this._commitPosition(ev.target.value);
    });

    root.querySelector(".sc-number-input").addEventListener("change", (ev) => {
      const committed = this._commitPosition(ev.target.value);
      if (committed !== null) {
        // Echo back what was actually sent, so a typed "150" or "-4"
        // shows as the clamped value the cover was given.
        ev.target.value = committed;
      }
    });
    root.querySelectorAll(".sc-preset").forEach((btn) => {
      btn.addEventListener("click", () =>
        this._callCoverService("set_cover_position", {
          position: parseInt(btn.dataset.position, 10),
        })
      );
    });
    root.querySelector(".sc-close-completely").addEventListener("click", () =>
      this._callService("close_completely", {})
    );
    // _syncing marks the writes _update() makes to these switches, so
    // the handlers act only on a user's toggle. <ha-switch> is a custom
    // element, not a native checkbox: assigning .checked on it is not
    // guaranteed to be event-silent.
    root.querySelector(".sc-toggle-recal").addEventListener("change", (ev) => {
      if (this._syncing) {
        return;
      }
      this._callService("set_recalibration_enabled", { enabled: ev.target.checked });
    });
    root.querySelector(".sc-toggle-rain").addEventListener("change", (ev) => {
      if (this._syncing) {
        return;
      }
      this._callService("set_rain_control_enabled", { enabled: ev.target.checked });
    });
    root.querySelector(".sc-btn-recal-manual").addEventListener("click", () =>
      this._callService("recalibrate_manual", {})
    );
    root.querySelector(".sc-btn-rain-protect").addEventListener("click", () =>
      this._callService("activate_rain_protection", {})
    );

    this._built = true;
  }

  _update() {
    if (!this._hass || !this._entityId) {
      return;
    }
    const stateObj = this._hass.states[this._entityId];
    if (!stateObj) {
      this._renderError(`Entity not found: ${this._entityId}`);
      return;
    }
    const attrs = stateObj.attributes;
    if (!this._built) {
      this._presets =
        this._configPresets ||
        (Array.isArray(attrs.presets) ? attrs.presets : null) ||
        DEFAULT_PRESETS;
      this._build();
    }

    const root = this.shadowRoot;
    const position = attrs.current_position;

    root.querySelector(".sc-name-text").textContent = attrs.friendly_name || this._entityId;
    root.querySelector(".sc-position").textContent =
      position !== null && position !== undefined ? `${position}%` : stateObj.state;

    // A cover whose position isn't known yet shows 0 on the controls
    // rather than leaving them blank; the "%" readout above still shows
    // the entity's own state so nothing is misrepresented as "closed".
    const controlValue = position !== null && position !== undefined ? position : 0;
    this._syncControl(".sc-slider", controlValue);
    this._syncControl(".sc-number-input", controlValue);

    root.querySelectorAll(".sc-preset").forEach((btn) => {
      btn.classList.toggle("active", parseInt(btn.dataset.position, 10) === position);
    });

    const unavailable = stateObj.state === "unavailable";
    const rootEl = root.querySelector(".sc-root");
    rootEl.classList.toggle("sc-unavailable", unavailable);

    const rainEl = root.querySelector(".sc-rain");
    rainEl.textContent = attrs.is_raining ? "🌧️ Raining" : "☀️ No rain";
    rainEl.classList.toggle("active", !!attrs.is_raining);

    // User-facing wording only: the explicit position (closed / vasistas /
    // open). A cached card talking to an older entity, or one whose
    // sensors haven't reported yet, has no window_position — fall back to
    // "unknown" rather than leaving the row blank.
    const windowEl = root.querySelector(".sc-window");
    const windowPosition = attrs.window_position;
    const windowLabel =
      WINDOW_LABELS[windowPosition] || WINDOW_LABELS.unknown;
    windowEl.textContent = `🪟 ${windowLabel}`;

    const recalEl = root.querySelector(".sc-recal-running");
    recalEl.textContent = attrs.recalibration_in_progress ? "⏳ Recalibrating…" : "";
    recalEl.classList.toggle("active", !!attrs.recalibration_in_progress);

    this._syncToggle(".sc-toggle-recal", !!attrs.recalibration_enabled);
    this._syncToggle(".sc-toggle-rain", !!attrs.rain_control_enabled);

    root
      .querySelector(".sc-btn-recal-manual")
      .toggleAttribute("disabled", !!attrs.recalibration_in_progress);
  }
}

/** Lovelace card: type: custom:smart-cover-card / entity: cover.xxx */
class SmartCoverCard extends SmartCoverElementBase {
  get _wrapInCard() {
    return true;
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("Set 'entity' to a Smart Cover entity, e.g. cover.my_shutter");
    }
    this._entityId = config.entity;
    this._configPresets = Array.isArray(config.presets) ? config.presets : null;
    this._hideSlider = !!config.hide_slider;
    this._built = false;
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig(hass) {
    // Entities are regular cover.* entities; find one by its Smart Cover
    // marker attribute rather than by domain.
    const first = Object.keys(hass.states).find(
      (e) => e.startsWith("cover.") && "recalibration_enabled" in hass.states[e].attributes
    );
    return { entity: first || "cover.example" };
  }
}

customElements.define("smart-cover-card", SmartCoverCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "smart-cover-card",
  name: "Smart Cover",
  description: "Cover controls plus recalibration/rain-protection toggles in a single card",
});
