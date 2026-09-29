/*
 * GeoPulse timeline card for Home Assistant.
 *
 * Map + stay/trip list for a date range, for the GeoPulse account owner and
 * every friend sharing their timeline. Data comes from the integration's
 * `geopulse/timeline` websocket command (admin-only); the GeoPulse token
 * never reaches the browser. Served by the integration itself, with Leaflet
 * vendored alongside it.
 *
 *   type: custom:geopulse-card
 *   title: Timeline        # optional
 *   days: 1                # optional, initial range ending today (1-31)
 *   map_height: 320        # optional, px
 *   entry_id: ...          # optional, if more than one GeoPulse entry
 *   tile_url: ...          # optional, raster tile URL template
 *   tile_attribution: ...  # optional, required by most tile providers
 */

const BASE = "/geopulse_frontend";
const VERSION = "0.1.0";
const MAX_DAYS = 31;
// OpenStreetMap's standard raster tiles - what HA's own frontend falls back
// to. (CARTO's basemaps now return "API key required" placeholder tiles.)
const DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const DEFAULT_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const MOVEMENT_ICONS = {
  WALK: "mdi:walk",
  RUNNING: "mdi:run",
  BICYCLE: "mdi:bike",
  CAR: "mdi:car",
  MOTORCYCLE: "mdi:motorbike",
  TRAIN: "mdi:train",
  FLIGHT: "mdi:airplane",
  BOAT: "mdi:ferry",
  UNKNOWN: "mdi:map-marker-path",
};

let leafletPromise;
function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (!leafletPromise) {
    leafletPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `${BASE}/vendor/leaflet.js`;
      script.onload = () => resolve(window.L);
      script.onerror = () => {
        leafletPromise = undefined;
        reject(new Error("Could not load the map library"));
      };
      document.head.appendChild(script);
    });
  }
  return leafletPromise;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function addDays(isoDate, days) {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

function daysBetween(a, b) {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86400000);
}

function formatDuration(seconds) {
  const minutes = Math.round((seconds || 0) / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

function formatDistance(meters, useMetric) {
  if (useMetric) return meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${meters} m`;
  const miles = meters / 1609.344;
  return miles >= 0.1 ? `${miles.toFixed(1)} mi` : `${Math.round(meters * 3.281)} ft`;
}

class GeoPulseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hidden = new Set(); // user_ids toggled off
    this._data = null;
    this._error = null;
    this._loading = false;
    this._map = null;
    this._layers = null;
    this._tileLayer = null;
    this._darkMode = null;
    this._requestId = 0;
  }

  static getStubConfig() {
    return { days: 1 };
  }

  setConfig(config) {
    const days = Number(config.days ?? 1);
    if (!Number.isInteger(days) || days < 1 || days > MAX_DAYS) {
      throw new Error(`days must be a whole number from 1 to ${MAX_DAYS}`);
    }
    this._config = { map_height: 320, ...config, days };
    this._range = null; // re-derive from config on next hass
    this._built = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._config) return;
    if (!this._range) {
      const end = this._today();
      this._range = { start: addDays(end, 1 - this._config.days), end };
    }
    if (!this._built) this._build();
    if (this._map && this._darkMode !== Boolean(hass.themes?.darkMode)) this._setTiles();
    if (first || !this._data) {
      if (!this._loading && !this._error) this._load();
    }
  }

  getCardSize() {
    return 9;
  }

  connectedCallback() {
    if (this._map) setTimeout(() => this._map && this._map.invalidateSize(), 0);
  }

  disconnectedCallback() {
    this._resizeObserver?.disconnect();
  }

  // --- helpers -------------------------------------------------------------

  get _timeZone() {
    return this._hass?.config?.time_zone;
  }

  get _language() {
    return this._hass?.locale?.language || this._hass?.language || navigator.language;
  }

  _today() {
    // en-CA formats as YYYY-MM-DD; evaluated in HA's time zone, not the browser's.
    return new Intl.DateTimeFormat("en-CA", { timeZone: this._timeZone }).format(new Date());
  }

  _formatTime(iso, withDate) {
    const options = { hour: "2-digit", minute: "2-digit", timeZone: this._timeZone };
    if (withDate) Object.assign(options, { weekday: "short", day: "numeric", month: "short" });
    return new Intl.DateTimeFormat(this._language, options).format(new Date(iso));
  }

  _dayKey(iso) {
    return new Intl.DateTimeFormat("en-CA", { timeZone: this._timeZone }).format(new Date(iso));
  }

  _formatDay(dayKey) {
    return new Intl.DateTimeFormat(this._language, {
      weekday: "long", day: "numeric", month: "long", timeZone: "UTC",
    }).format(new Date(`${dayKey}T12:00:00Z`));
  }

  get _metric() {
    return this._hass?.config?.unit_system?.length !== "mi";
  }

  // --- structure -----------------------------------------------------------

  _build() {
    const root = this.shadowRoot;
    root.replaceChildren();
    root.append(
      el("link", { rel: "stylesheet", href: `${BASE}/vendor/leaflet.css` }),
      el("style", {}, STYLES)
    );
    this._startInput = el("input", { type: "date", "aria-label": "From", onchange: () => this._onDateInput() });
    this._endInput = el("input", { type: "date", "aria-label": "To", onchange: () => this._onDateInput() });
    this._peopleBar = el("div", { class: "people" });
    this._status = el("div", { class: "status", role: "status" });
    this._mapEl = el("div", { class: "map", style: `height:${Number(this._config.map_height)}px` });
    this._list = el("div", { class: "list" });

    const quick = (label, days, offset = 0) =>
      el("button", { type: "button", class: "chip", onclick: () => {
        const end = addDays(this._today(), -offset);
        this._setRange(addDays(end, 1 - days), end);
      } }, label);

    root.append(
      el("ha-card", {},
        this._config.title ? el("h1", { class: "card-header" }, this._config.title) : null,
        el("div", { class: "controls" },
          el("div", { class: "quick" }, quick("Today", 1), quick("Yesterday", 1, 1), quick("7 days", 7)),
          el("div", { class: "dates" },
            el("button", { type: "button", class: "icon", "aria-label": "Previous", onclick: () => this._shift(-1) },
              el("ha-icon", { icon: "mdi:chevron-left" })),
            this._startInput, el("span", { class: "sep" }, "–"), this._endInput,
            el("button", { type: "button", class: "icon", "aria-label": "Next", onclick: () => this._shift(1) },
              el("ha-icon", { icon: "mdi:chevron-right" }))
          )
        ),
        this._peopleBar,
        this._status,
        this._mapEl,
        this._list,
      )
    );
    this._built = true;
    this._syncInputs();
    this._initMap();
  }

  async _initMap() {
    try {
      const L = await loadLeaflet();
      if (this._map) return;
      this._map = L.map(this._mapEl, { zoomControl: true, attributionControl: true })
        .setView([this._hass?.config?.latitude || 0, this._hass?.config?.longitude || 0], 11);
      this._layers = L.featureGroup().addTo(this._map);
      this._setTiles();
      this._resizeObserver = new ResizeObserver(() => this._map.invalidateSize());
      this._resizeObserver.observe(this._mapEl);
      if (this._data) this._render();
    } catch (err) {
      this._mapEl.replaceChildren(el("div", { class: "map-error" }, err.message));
    }
  }

  _setTiles() {
    const L = window.L;
    this._darkMode = Boolean(this._hass?.themes?.darkMode);
    const custom = Boolean(this._config.tile_url);
    // OSM has no dark raster style; dim it with a CSS filter instead. Only for
    // the default tiles - a custom tile server may already be dark.
    this._mapEl.classList.toggle("dark", this._darkMode && !custom);
    if (this._tileLayer) return;
    this._tileLayer = L.tileLayer(this._config.tile_url || DEFAULT_TILES, {
      maxZoom: 19,
      attribution: custom ? this._config.tile_attribution || "" : DEFAULT_ATTRIBUTION,
    }).addTo(this._map);
  }

  // --- range ---------------------------------------------------------------

  _syncInputs() {
    const today = this._today();
    this._startInput.value = this._range.start;
    this._endInput.value = this._range.end;
    this._startInput.max = today;
    this._endInput.max = today;
  }

  _onDateInput() {
    let { value: start } = this._startInput;
    let { value: end } = this._endInput;
    if (!start || !end) return;
    if (end < start) [start, end] = [end, start];
    if (daysBetween(start, end) >= MAX_DAYS) start = addDays(end, 1 - MAX_DAYS);
    this._setRange(start, end);
  }

  _shift(direction) {
    const span = daysBetween(this._range.start, this._range.end) + 1;
    let end = addDays(this._range.end, direction * span);
    const today = this._today();
    if (end > today) end = today;
    this._setRange(addDays(end, 1 - span), end);
  }

  _setRange(start, end) {
    this._range = { start, end };
    this._syncInputs();
    this._load();
  }

  // --- data ----------------------------------------------------------------

  async _load() {
    const requestId = ++this._requestId;
    this._loading = true;
    this._error = null;
    this._status.replaceChildren(el("span", { class: "muted" }, "Loading…"));
    try {
      const message = {
        type: "geopulse/timeline",
        start_date: this._range.start,
        end_date: this._range.end,
      };
      if (this._config.entry_id) message.entry_id = this._config.entry_id;
      const data = await this._hass.callWS(message);
      if (requestId !== this._requestId) return; // a newer range was picked
      this._data = data;
    } catch (err) {
      if (requestId !== this._requestId) return;
      this._data = null;
      this._error =
        err?.code === "unauthorized" && /admin/i.test(err?.message || "")
          ? "Only Home Assistant administrators can view GeoPulse history."
          : err?.message || String(err);
    } finally {
      if (requestId === this._requestId) this._loading = false;
    }
    this._render();
  }

  // --- rendering -----------------------------------------------------------

  _visiblePeople() {
    return (this._data?.people || []).filter((p) => !this._hidden.has(p.user_id));
  }

  _render() {
    this._mapEl.hidden = Boolean(this._error);
    if (this._error) {
      this._status.replaceChildren(el("div", { class: "error" }, this._error));
      this._peopleBar.replaceChildren();
      this._list.replaceChildren();
      this._layers?.clearLayers();
      return;
    }
    if (!this._data) return;
    this._status.replaceChildren();
    this._renderPeople();
    this._renderMap();
    this._renderList();
  }

  _renderPeople() {
    const people = this._data.people;
    this._peopleBar.replaceChildren(
      ...people.map((person) => {
        const off = this._hidden.has(person.user_id);
        return el("button", {
          type: "button",
          class: `chip person${off ? " off" : ""}`,
          "aria-pressed": String(!off),
          title: off ? "Show" : "Hide",
          onclick: () => {
            off ? this._hidden.delete(person.user_id) : this._hidden.add(person.user_id);
            this._render();
          },
        },
        el("span", { class: "dot", style: `background:${person.color}` }),
        person.is_self ? `${person.name} (you)` : person.name,
        el("span", { class: "muted" }, ` · ${formatDistance(person.distance, this._metric)}`));
      })
    );
  }

  _renderMap() {
    const L = window.L;
    if (!this._map || !L) return;
    this._layers.clearLayers();
    for (const person of this._visiblePeople()) {
      for (const segment of person.path) {
        if (segment.length > 1) {
          L.polyline(segment, { color: person.color, weight: 3, opacity: 0.8 }).addTo(this._layers);
        }
      }
      for (const stay of person.stays) {
        const end = new Date(Date.parse(stay.start) + stay.duration * 1000).toISOString();
        L.circleMarker(stay.location, {
          radius: 6, color: "#fff", weight: 2, fillColor: person.color, fillOpacity: 1,
        })
          .bindTooltip(
            `<b>${escapeHtml(stay.name || "Stay")}</b><br>${escapeHtml(person.name)} · ` +
              `${escapeHtml(this._formatTime(stay.start))}–${escapeHtml(this._formatTime(end))} ` +
              `(${escapeHtml(formatDuration(stay.duration))})`
          )
          .addTo(this._layers);
      }
    }
    const bounds = this._layers.getBounds();
    if (bounds.isValid()) this._map.fitBounds(bounds, { padding: [24, 24], maxZoom: 16 });
    this._map.invalidateSize();
  }

  _renderList() {
    const multiDay = this._range.start !== this._range.end;
    const multiPerson = this._visiblePeople().length > 1;
    const items = [];
    for (const person of this._visiblePeople()) {
      for (const stay of person.stays) items.push({ kind: "stay", person, ...stay });
      for (const trip of person.trips) items.push({ kind: "trip", person, ...trip });
      for (const gap of person.gaps) {
        if (gap.start && gap.end) {
          items.push({
            kind: "gap", person, start: gap.start,
            duration: (Date.parse(gap.end) - Date.parse(gap.start)) / 1000,
          });
        }
      }
    }
    items.sort((a, b) => Date.parse(a.start) - Date.parse(b.start));

    if (!items.length) {
      this._list.replaceChildren(el("div", { class: "empty muted" }, "No stays or trips in this range."));
      return;
    }
    const rows = [];
    let day = null;
    for (const item of items) {
      const key = this._dayKey(item.start);
      if (multiDay && key !== day) {
        day = key;
        rows.push(el("div", { class: "day" }, this._formatDay(key)));
      }
      rows.push(this._row(item, multiPerson));
    }
    this._list.replaceChildren(...rows);
  }

  _row(item, multiPerson) {
    const end = new Date(Date.parse(item.start) + item.duration * 1000).toISOString();
    const when = `${this._formatTime(item.start)} – ${this._formatTime(end)}`;
    let icon, title, detail;
    if (item.kind === "stay") {
      icon = "mdi:map-marker";
      title = item.name || "Stay";
      detail = formatDuration(item.duration);
    } else if (item.kind === "trip") {
      const movement = item.movement || "UNKNOWN";
      icon = MOVEMENT_ICONS[movement] || MOVEMENT_ICONS.UNKNOWN;
      title = movement === "UNKNOWN" ? "Trip" : movement.charAt(0) + movement.slice(1).toLowerCase();
      detail = `${formatDistance(item.distance, this._metric)} · ${formatDuration(item.duration)}`;
    } else {
      icon = "mdi:help-circle-outline";
      title = "No data";
      detail = formatDuration(item.duration);
    }
    const focus = () => {
      if (!this._map || !window.L || item.kind === "gap") return;
      if (item.kind === "stay") this._map.setView(item.location, 16);
      else this._map.fitBounds(window.L.latLngBounds([item.from, item.to]), { padding: [32, 32], maxZoom: 16 });
      this._mapEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    return el("button", {
      type: "button", class: `row ${item.kind}`, onclick: focus, disabled: item.kind === "gap",
    },
      el("span", { class: "marker", style: `color:${item.person.color}` }, el("ha-icon", { icon })),
      el("span", { class: "main" },
        el("span", { class: "title" }, title),
        el("span", { class: "muted" }, multiPerson ? `${item.person.name} · ${detail}` : detail)),
      el("span", { class: "when muted" }, when));
  }
}

const STYLES = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .card-header { margin: 0; padding: 16px 16px 0; font-size: 24px; font-weight: 400;
    color: var(--ha-card-header-color, var(--primary-text-color)); }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    justify-content: space-between; padding: 12px 16px 4px; }
  .quick, .dates, .people { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .people { padding: 4px 16px 8px; }
  .chip { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 16px; cursor: pointer;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
    display: inline-flex; align-items: center; gap: 6px; }
  .chip:hover { background: var(--secondary-background-color); }
  .chip.off { opacity: 0.45; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .icon { background: none; border: none; cursor: pointer; color: var(--primary-text-color);
    padding: 2px; border-radius: 50%; display: inline-flex; }
  .icon:hover { background: var(--secondary-background-color); }
  input[type=date] { font: inherit; font-size: 13px; padding: 3px 6px; border-radius: 6px;
    border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color); color-scheme: light dark; }
  .sep { color: var(--secondary-text-color); }
  .status:empty { display: none; }
  .status { padding: 0 16px 8px; }
  .error { color: var(--error-color, #db4437); }
  .muted { color: var(--secondary-text-color); }
  .map { width: 100%; background: var(--secondary-background-color); z-index: 0; }
  .map[hidden] { display: none; }
  .map.dark .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.9) contrast(0.9); }
  .map-error { padding: 16px; color: var(--secondary-text-color); }
  .list { max-height: 360px; overflow-y: auto; padding: 4px 0 8px; }
  .day { padding: 12px 16px 4px; font-weight: 500; font-size: 14px; color: var(--primary-text-color); }
  .row { display: flex; width: 100%; gap: 12px; align-items: center; padding: 6px 16px;
    font: inherit; font-size: 14px; text-align: left; border: none; background: none;
    color: var(--primary-text-color); cursor: pointer; }
  .row:hover:not(:disabled) { background: var(--secondary-background-color); }
  .row:disabled { cursor: default; opacity: 0.7; }
  .row .marker { flex: none; display: inline-flex; }
  .row .main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .row .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .when { flex: none; font-size: 13px; font-variant-numeric: tabular-nums; }
  .row .muted { font-size: 13px; }
  .empty { padding: 16px; text-align: center; }
  .leaflet-container { font: inherit; }
`;

if (!customElements.get("geopulse-card")) {
  customElements.define("geopulse-card", GeoPulseCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "geopulse-card",
    name: "GeoPulse timeline",
    description: "Map and stay/trip list from GeoPulse for a date range.",
    documentationURL: "https://github.com/YOUR_GITHUB_USERNAME/ha-geopulse",
  });
  console.info(`%c GEOPULSE-CARD %c ${VERSION} `, "background:#0f766e;color:#fff", "");
}
