/*
 * GeoPulse timeline card for Home Assistant.
 *
 * Map + stay/trip list for a date range, for the GeoPulse account owner and
 * every friend sharing their timeline. Data comes from the integration's
 * `geopulse/timeline` websocket command; the GeoPulse token never reaches
 * the browser. Who may view it is set in the integration's settings. Served by the integration itself, with Leaflet
 * vendored alongside it.
 *
 *   type: custom:geopulse-card
 *   title: Timeline            # optional
 *   users: [<user id>, ...]    # optional, people to show (default: everyone)
 *   hidden_users: [...]        # optional, shown as chips but toggled off
 *   colors: {<user id>: "#hex"}  # optional, per-person colours (default: GeoPulse's)
 *   default_range: today       # today | yesterday | last_7_days | last_30_days | last_n_days
 *   days: 3                    # for last_n_days (1-31); alone, implies last_n_days
 *   sections: [filters, map, list]  # which parts to show, in this order
 *   layout: auto               # auto | stacked | columns (map and list side by side)
 *   map_height: 320            # px, when the card isn't filling a panel/grid
 *   show_current: true         # "now" marker per person when the range includes today
 *   pulse: auto                # now-marker animation: auto (follows reduce-motion) | always | off
 *   refresh_interval: 120      # s, auto-refresh while the range includes today (0 = off)
 *   entry_id: ...              # optional, if more than one GeoPulse entry
 *   tile_url: ...              # optional, raster tile URL template
 *   tile_attribution: ...      # optional, required by most tile providers
 *
 * `layout: auto` goes side by side once the card is COLUMNS_MIN_WIDTH wide, so
 * one card works as a small tile and as a full-screen view. In a panel view,
 * or a sections view with the card's rows set, it fills the height it's
 * given. A visual editor (geopulse-card-editor) covers all of these.
 */

const BASE = "/geopulse_frontend";
const VERSION = "0.1.0";
const MAX_DAYS = 31;
// OpenStreetMap's standard raster tiles - what HA's own frontend falls back
// to. (CARTO's basemaps now return "API key required" placeholder tiles.)
const DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const DEFAULT_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// default_range presets: [days, days back from today for the last day].
const RANGES = {
  today: [1, 0],
  yesterday: [1, 1],
  last_7_days: [7, 0],
  last_30_days: [30, 0],
};
const SECTIONS = ["filters", "map", "list"];
const LAYOUTS = ["auto", "stacked", "columns"];
const COLUMNS_MIN_WIDTH = 700;
const DEFAULTS = {
  default_range: "today",
  sections: SECTIONS,
  layout: "auto",
  map_height: 320,
  show_current: true,
  pulse: "auto",
  refresh_interval: 120,
};
const PULSE_MODES = ["auto", "always", "off"];

const HEX_COLOR = /^#[0-9a-f]{3,8}$/i;
// "Now" markers closer than this on screen are drawn as one group marker.
const GROUP_PX = 20;

function hexToRgb(hex) {
  let h = hex.slice(1);
  if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

function rgbToHex(rgb) {
  return `#${rgb.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

function colorMap(value) {
  if (value === undefined || value === null) return {};
  if (typeof value !== "object" || Array.isArray(value)) throw new Error("colors must map GeoPulse user ids to #hex colours");
  for (const [id, color] of Object.entries(value)) {
    // These end up in styles and marker HTML: hex only.
    if (typeof color !== "string" || !HEX_COLOR.test(color)) {
      throw new Error(`colors: "${id}" must be a #hex colour`);
    }
  }
  return { ...value };
}

function stringList(value, name) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.some((v) => typeof v !== "string")) {
    throw new Error(`${name} must be a list of GeoPulse user ids`);
  }
  return value;
}

// `sections` lists what to show, in order. Older `show_filters/show_map/
// show_list: false` configs map onto it.
function sectionsOf(config) {
  if (config.sections !== undefined) {
    const list = config.sections;
    if (!Array.isArray(list) || list.some((s) => !SECTIONS.includes(s)) || new Set(list).size !== list.length) {
      throw new Error(`sections must list some of: ${SECTIONS.join(", ")} (each once)`);
    }
    if (!list.length) throw new Error("sections needs at least one of: filters, map, list");
    return list;
  }
  return SECTIONS.filter((s) => config[`show_${s}`] !== false);
}

function normalizeConfig(config) {
  const out = { ...DEFAULTS, ...config };
  if (config.days !== undefined && config.default_range === undefined) {
    out.default_range = "last_n_days"; // YAML shorthand: `days: N` alone
  }
  if (out.default_range === "last_n_days") {
    const days = Number(out.days ?? 3);
    if (!Number.isInteger(days) || days < 1 || days > MAX_DAYS) {
      throw new Error(`days must be a whole number from 1 to ${MAX_DAYS}`);
    }
    out.days = days;
    out.range = [days, 0];
  } else if (RANGES[out.default_range]) {
    out.range = RANGES[out.default_range];
  } else {
    throw new Error(
      `default_range must be one of: ${[...Object.keys(RANGES), "last_n_days"].join(", ")}`
    );
  }
  out.users = stringList(config.users, "users");
  out.hidden_users = stringList(config.hidden_users, "hidden_users");
  out.colors = colorMap(config.colors);
  out.sections = sectionsOf(config);
  out.show_current = out.show_current !== false;
  if (!PULSE_MODES.includes(out.pulse)) throw new Error(`pulse must be one of: ${PULSE_MODES.join(", ")}`);
  const refresh = Number(out.refresh_interval);
  if (!Number.isFinite(refresh) || refresh < 0) throw new Error("refresh_interval must be 0 (off) or seconds");
  out.refresh_interval = refresh === 0 ? 0 : Math.max(30, refresh); // don't hammer GeoPulse
  if (!LAYOUTS.includes(out.layout)) throw new Error(`layout must be one of: ${LAYOUTS.join(", ")}`);
  return out;
}

const ERROR_MESSAGES = {
  unauthorized:
    "You don't have access to this GeoPulse timeline. An administrator can add you under the GeoPulse integration's Configure → Settings.",
  geopulse_auth_failed:
    "GeoPulse rejected the integration's API token. Re-authenticate the GeoPulse integration.",
  geopulse_unavailable: "GeoPulse can't be reached right now.",
  not_found: "The GeoPulse integration isn't set up or isn't loaded.",
};

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

function timeAgo(iso) {
  const minutes = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h ${minutes % 60} min ago` : `${Math.floor(hours / 24)} d ago`;
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
    return { default_range: "today" };
  }

  static getConfigElement() {
    return document.createElement("geopulse-card-editor");
  }

  setConfig(config) {
    this._config = normalizeConfig(config);
    this._hidden = new Set(this._config.hidden_users);
    this._range = null; // re-derive from config on next hass
    this._data = null;
    this._error = null;
    this._requestId++; // drop any in-flight load for the previous config
    this._loading = false;
    this._built = false;
    this._map?.remove();
    this._map = null;
    this._tileLayer = null;
    this._resizeObserver?.disconnect();
    this._hostObserver?.disconnect();
    if (this._hass) this.hass = this._hass; // editor preview: rebuild now
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._config) return;
    if (!this._range) {
      const [days, back] = this._config.range;
      const end = addDays(this._today(), -back);
      this._range = { start: addDays(end, 1 - days), end };
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
    if (this._built) {
      this._hostObserver?.observe(this);
      this._applyLayout();
    }
    if (this._map) setTimeout(() => this._map && this._map.invalidateSize(), 0);
    if (this._data) this._scheduleRefresh();
  }

  disconnectedCallback() {
    this._resizeObserver?.disconnect();
    this._hostObserver?.disconnect();
    clearTimeout(this._refreshTimer);
    if (this._onWindowResize) window.removeEventListener("resize", this._onWindowResize);
    this._onWindowResize = null;
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
    const { sections } = this._config;
    const has = (name) => sections.includes(name);
    this._startInput = el("input", { type: "date", "aria-label": "From", onchange: () => this._onDateInput() });
    this._endInput = el("input", { type: "date", "aria-label": "To", onchange: () => this._onDateInput() });
    this._peopleBar = has("filters") ? el("div", { class: "people" }) : null;
    this._status = el("div", { class: "status", role: "status" });
    this._mapEl = has("map") ? el("div", { class: "map" }) : null;
    this._list = has("list") ? el("div", { class: "list" }) : null;

    const quick = (label, days, offset = 0) =>
      el("button", { type: "button", class: "chip", onclick: () => {
        const end = addDays(this._today(), -offset);
        this._setRange(addDays(end, 1 - days), end);
      } }, label);

    const parts = {
      filters: has("filters") && el("div", { class: "filters" },
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
        this._peopleBar),
      map: this._mapEl,
      list: this._list,
    };
    // Grid placement for the side-by-side layout: filters span the top (or
    // the bottom if listed last); map and list take the columns in the
    // configured order. The stacked layout just follows DOM order.
    const panes = sections.filter((name) => name !== "filters");
    const filtersLast = sections[sections.length - 1] === "filters" && sections.length > 1;
    sections.forEach((name, index) => {
      const node = parts[name];
      node.style.order = String(index);
      if (name === "filters") node.dataset.row = filtersLast ? "bottom" : "top";
      else node.dataset.col = String(panes.indexOf(name) + 1);
    });
    this._body = el("div", { class: `body${panes.length === 2 ? " two" : ""}` },
      ...sections.map((name) => parts[name]));

    this._cardEl = el("ha-card", {},
      this._config.title ? el("h1", { class: "card-header" }, this._config.title) : null,
      this._status,
      this._body,
    );
    root.append(this._cardEl);
    this._built = true;
    this._applyLayout();
    this._hostObserver?.disconnect();
    this._hostObserver = new ResizeObserver(() => this._applyLayout());
    this._hostObserver.observe(this);
    this._syncInputs();
    if (has("map")) this._initMap();
  }

  set layout(value) {
    // Set by HA: "panel" in a panel view, "grid" in a sections view.
    this._hostLayout = value;
    this._applyLayout();
  }

  get layout() {
    return this._hostLayout;
  }

  get _fills() {
    // Fill the given height in a panel, or in a sections-view grid cell whose
    // rows were set explicitly; otherwise size to content.
    const rows = this._config?.grid_options?.rows;
    return this._hostLayout === "panel" || (this._hostLayout === "grid" && typeof rows === "number");
  }

  _applyLayout() {
    if (!this._built) return;
    const { layout } = this._config;
    this._fitPanelHeight();
    // Side by side only makes sense with both map and list shown.
    const columns =
      this._body.classList.contains("two") &&
      (layout === "columns" || (layout === "auto" && this.getBoundingClientRect().width >= COLUMNS_MIN_WIDTH));
    const fill = this._fills;
    this.classList.toggle("fill", fill);
    this._body.classList.toggle("columns", columns);
    this._body.classList.toggle("stacked", !columns);
    // Fixed map height unless the card is filling a panel/grid cell.
    if (this._mapEl) this._mapEl.style.height = fill ? "" : `${Number(this._config.map_height) || 320}px`;
    if (this._list) {
      // Side by side and not filling: match the map's height and scroll.
      this._list.style.height = columns && !fill && this._mapEl ? `${Number(this._config.map_height) || 320}px` : "";
    }
  }

  _fitPanelHeight() {
    // In a panel view the card should reach the bottom of the window. HA's
    // `height: 100%` chain isn't reliably definite there, and then the list
    // (in normal flow) sets the height - stretching the map instead of
    // scrolling. So measure: from the card's top to the bottom of the viewport.
    if (this._hostLayout !== "panel") {
      if (this._panelHeight) this.style.height = "";
      this._panelHeight = null;
      return;
    }
    const viewport = window.visualViewport?.height || window.innerHeight;
    const top = this.getBoundingClientRect().top + window.scrollY;
    const height = Math.max(320, Math.floor(viewport - top));
    if (height !== this._panelHeight) {
      this._panelHeight = height;
      this.style.height = `${height}px`;
    }
    if (!this._onWindowResize) {
      this._onWindowResize = () => this._applyLayout();
      window.addEventListener("resize", this._onWindowResize);
    }
  }

  getGridOptions() {
    return { columns: 12, rows: "auto", min_columns: 6, min_rows: 4 };
  }

  async _initMap() {
    try {
      const L = await loadLeaflet();
      if (this._map) return;
      this._map = L.map(this._mapEl, { zoomControl: true, attributionControl: true })
        .setView([this._hass?.config?.latitude || 0, this._hass?.config?.longitude || 0], 11);
      this._layers = L.featureGroup().addTo(this._map);
      this._nowLayer = L.featureGroup().addTo(this._map);
      // Grouping depends on screen distance, so regroup after every zoom.
      this._map.on("zoomend", () => this._renderCurrent());
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
      // Home Assistant serves pages with Referrer-Policy: no-referrer, and
      // OSM's tile policy blocks requests without a Referer ("Access
      // blocked" tiles). Send just the origin - no path - for OSM tiles only.
      referrerPolicy: custom ? false : "origin",
    }).addTo(this._map);
  }

  // --- range ---------------------------------------------------------------

  _syncInputs() {
    if (!this._config.sections.includes("filters")) return;
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

  async _load({ refresh = false } = {}) {
    const requestId = ++this._requestId;
    this._loading = true;
    this._error = null;
    if (!refresh) {
      this._fitted = false; // new range: fit the map to it once loaded
      this._status.replaceChildren(el("span", { class: "muted" }, "Loading…"));
    }
    try {
      const message = {
        type: "geopulse/timeline",
        start_date: this._range.start,
        end_date: this._range.end,
      };
      if (this._config.entry_id) message.entry_id = this._config.entry_id;
      if (this._config.users.length) message.user_ids = this._config.users;
      const data = await this._hass.callWS(message);
      if (requestId !== this._requestId) return; // a newer range was picked
      // Per-person colour overrides; GeoPulse's colour otherwise.
      for (const person of data.people || []) {
        person.color = this._config.colors[person.user_id] || person.color;
      }
      this._data = data;
    } catch (err) {
      if (requestId !== this._requestId) return;
      this._data = null;
      this._error = ERROR_MESSAGES[err?.code] || err?.message || String(err);
    } finally {
      if (requestId === this._requestId) this._loading = false;
    }
    this._render();
    this._scheduleRefresh();
  }

  _scheduleRefresh() {
    clearTimeout(this._refreshTimer);
    const seconds = this._config.refresh_interval;
    // Only while the range includes now; past ranges don't change.
    if (!seconds || !this._data?.includes_now || !this.isConnected) return;
    this._refreshTimer = setTimeout(() => {
      if (!document.hidden) this._load({ refresh: true });
      else this._scheduleRefresh();
    }, seconds * 1000);
  }

  // --- rendering -----------------------------------------------------------

  _visiblePeople() {
    return (this._data?.people || []).filter((p) => !this._hidden.has(p.user_id));
  }

  _render() {
    if (this._mapEl) this._mapEl.hidden = Boolean(this._error);
    if (this._error) {
      this._status.replaceChildren(el("div", { class: "error" }, this._error));
      this._peopleBar?.replaceChildren();
      this._list?.replaceChildren();
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
    if (!this._peopleBar) return;
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
        person.name,
        el("span", { class: "muted" }, ` · ${formatDistance(person.distance, this._metric)}`));
      })
    );
  }

  _renderMap() {
    const L = window.L;
    if (!this._mapEl || !this._map || !L) return;
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
    this._renderCurrent();
    // Fit once per range; auto-refreshes keep the user's pan/zoom.
    const bounds = this._layers.getBounds();
    const nowBounds = this._nowLayer.getBounds();
    if (nowBounds.isValid()) bounds.extend(nowBounds);
    if (!this._fitted && bounds.isValid()) {
      this._map.fitBounds(bounds, { padding: [24, 24], maxZoom: 16 });
      this._fitted = true;
    }
    this._map.invalidateSize();
  }

  _renderCurrent() {
    const L = window.L;
    if (!this._nowLayer || !L) return;
    this._nowLayer.clearLayers();
    if (!this._config.show_current || !this._data) return;
    const people = this._visiblePeople().filter((p) => p.current?.location);
    // Greedy grouping by screen distance at the current zoom: people at (or
    // near) the same spot share one marker instead of hiding each other.
    const groups = [];
    for (const person of people) {
      const point = this._map.latLngToLayerPoint(person.current.location);
      const group = groups.find((g) => g.point.distanceTo(point) < GROUP_PX);
      if (group) group.people.push(person);
      else groups.push({ point, people: [person] });
    }
    for (const group of groups) {
      const members = group.people;
      const location = L.latLng(
        members.reduce((sum, p) => sum + p.current.location[0], 0) / members.length,
        members.reduce((sum, p) => sum + p.current.location[1], 0) / members.length
      );
      // Colours are hex-validated (server and config); nothing else goes in the HTML.
      const colors = members.map((p) => p.color);
      const fill = colors.length === 1
        ? colors[0]
        : `conic-gradient(${colors.map((c, i) => `${c} ${(i / colors.length) * 360}deg ${((i + 1) / colors.length) * 360}deg`).join(", ")})`;
      const size = members.length === 1 ? 28 : 34;
      const icon = L.divIcon({
        className: `now-marker pulse-${this._config.pulse}${members.length > 1 ? " now-group" : ""}`,
        html:
          `<span class="now-pulse" style="--c:${colors[0]}"></span>` +
          `<span class="now-dot" style="background:${fill}"></span>` +
          (members.length > 1 ? `<span class="now-count">${members.length}</span>` : ""),
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
      });
      const lines = members.map((p) => {
        const when = p.current.time;
        return `<span class="tt-dot" style="background:${p.color}"></span><b>${escapeHtml(p.name)}</b>` +
          (when ? ` · ${escapeHtml(this._formatTime(when))} (${escapeHtml(timeAgo(when))})` : "");
      });
      L.marker(location, { icon, zIndexOffset: 1000, keyboard: false })
        .bindTooltip(`${members.length > 1 ? `<b>${members.length} people here now</b><br>` : "Now<br>"}${lines.join("<br>")}`)
        .addTo(this._nowLayer);
    }
  }

  _renderList() {
    if (!this._list) return;
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
  :host(.fill) { height: 100%; }
  ha-card { overflow: hidden; }
  :host(.fill) ha-card { height: 100%; display: flex; flex-direction: column; }
  .body { display: flex; flex-direction: column; }
  :host(.fill) .body { flex: 1; min-height: 0; overflow: hidden; }
  :host(.fill) .body.stacked .map { flex: 1 1 auto; min-height: 200px; }
  :host(.fill) .body.stacked .list { flex: 0 1 auto; max-height: 45%; }
  .body.columns.two { display: grid; grid-template-columns: minmax(0, 3fr) minmax(260px, 2fr);
    grid-template-rows: auto minmax(0, 1fr) auto; }
  .body.columns.two > [data-row="top"] { grid-row: 1; grid-column: 1 / -1; }
  .body.columns.two > [data-row="bottom"] { grid-row: 3; grid-column: 1 / -1; }
  .body.columns.two > [data-col="1"] { grid-row: 2; grid-column: 1; }
  .body.columns.two > [data-col="2"] { grid-row: 2; grid-column: 2; }
  .body.columns.two > .list { max-height: none; }
  .body.columns.two > .list[data-col="2"] { border-left: 1px solid var(--divider-color); }
  .body.columns.two > .list[data-col="1"] { border-right: 1px solid var(--divider-color); }
  :host(.fill) .body.columns.two > .map { height: 100%; }
  :host(.fill) .body > .list { overflow-y: auto; min-height: 0; }
  :host(.fill) .body.columns.two > .list { height: 100%; max-height: none; }
  :host(.fill) .body.columns.two > * { min-height: 0; }
  .card-header { margin: 0; padding: 16px 16px 0; font-size: 24px; font-weight: 400;
    color: var(--ha-card-header-color, var(--primary-text-color)); }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    justify-content: space-between; padding: 12px 16px 4px; }
  .quick, .dates, .people { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .people { padding: 4px 16px 8px; }
  .filters[data-row="bottom"] { border-top: 1px solid var(--divider-color); padding-top: 4px; }
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
  .list { max-height: 360px; overflow-y: auto; padding: 4px 0 8px; box-sizing: border-box; }
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
  /* Own class names: .dot is the people-chip dot. No position rule on
     .now-marker: Leaflet places markers with position:absolute, and
     overriding it put later markers (friends) in normal flow - offset by the
     markers before them, a fixed pixel drift that looked zoom-dependent. */
  .now-group .now-dot { inset: 5px; }
  .now-count { position: absolute; right: -4px; top: -4px; min-width: 16px; height: 16px; padding: 0 3px;
    box-sizing: border-box; border-radius: 8px; background: #212121; color: #fff; font: 600 11px/16px sans-serif;
    text-align: center; box-shadow: 0 0 0 2px #fff; }
  .tt-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
  .now-dot { position: absolute; inset: 6px; border-radius: 50%; background: var(--c);
    border: 3px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,.3), 0 1px 5px rgba(0,0,0,.45); box-sizing: border-box; }
  .now-pulse { position: absolute; inset: 0; border-radius: 50%; background: var(--c); opacity: .35;
    animation: now-pulse 2s ease-out infinite; }
  @keyframes now-pulse { from { transform: scale(.5); opacity: .55; } to { transform: scale(1.8); opacity: 0; } }
  /* pulse: auto follows the device's reduce-motion setting; always/off force it.
     Without animation the halo stays as a static ring so it still reads as "live". */
  .pulse-off .now-pulse { animation: none; transform: scale(1.3); opacity: .3; }
  @media (prefers-reduced-motion: reduce) {
    .pulse-auto .now-pulse { animation: none; transform: scale(1.3); opacity: .3; }
  }
`;

// --- visual editor ----------------------------------------------------------

const RANGE_LABELS = {
  today: "Today",
  yesterday: "Yesterday",
  last_7_days: "Last 7 days",
  last_30_days: "Last 30 days",
  last_n_days: "Last N days",
};

const LABELS = {
  days: "Number of days",
  title: "Title",
  entry_id: "GeoPulse integration entry",
  users: "People to show",
  hidden_users: "Hidden by default",
  default_range: "Default date range",
  sections: "Sections, in order",
  layout: "Layout",
  map_height: "Map height",
  show_current: "Show current location",
  pulse: "Pulse animation",
  refresh_interval: "Auto-refresh",
  tile_url: "Tile URL",
  tile_attribution: "Tile attribution",
};

const HELPERS = {
  colors: "Pick GeoPulse's own colour again to go back to it.",
  entry_id: "Only needed with more than one GeoPulse entry.",
  users: "Leave empty to show everyone: you plus friends sharing their timeline with you.",
  hidden_users: "Listed in the filters but switched off until tapped.",
  tile_url: "Raster tiles, e.g. https://tiles.example.com/{z}/{x}/{y}.png. Default: OpenStreetMap.",
  sections: "Untick to hide a part; drag to change the order. Side by side, the filters sit on top — or at the bottom if they're last.",
  layout: `Auto puts the map and list side by side once the card is ${COLUMNS_MIN_WIDTH} px wide. In a panel view, or a sections view with the card's rows set, the card fills the height.`,
  map_height: "Used when the card isn't filling a panel or a sized grid cell.",
  show_current: "When the range includes today, mark where each person is now (friends: if they share their live location).",
  refresh_interval: "While the range includes today. 0 turns it off; minimum 30 s.",
  pulse: "Auto follows your device's reduce-motion setting (no pulse if it's on).",
};

const SECTION_LABELS = { filters: "Filters (date range and people)", map: "Map", list: "Stays and trips list" };
const LAYOUT_LABELS = { auto: "Auto", stacked: "Stacked", columns: "Side by side" };

class GeoPulseCardEditor extends HTMLElement {
  constructor() {
    super();
    this._users = null; // [{user_id, name, color, is_self}] once loaded
    this._usersError = null;
    this._usersFor = undefined; // entry_id the list was loaded for
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._usersFor !== (this._config?.entry_id || null)) this._loadUsers();
    this._render();
  }

  async _loadUsers() {
    const entryId = this._config?.entry_id || null;
    this._usersFor = entryId;
    try {
      const message = { type: "geopulse/users" };
      if (entryId) message.entry_id = entryId;
      const { users } = await this._hass.callWS(message);
      if (this._usersFor !== entryId) return;
      this._users = users;
      this._usersError = null;
    } catch (err) {
      if (this._usersFor !== entryId) return;
      this._users = [];
      this._usersError =
        err?.code === "unauthorized"
          ? "You're not on this GeoPulse timeline's viewer list; you can still enter user ids."
          : `Couldn't load GeoPulse users: ${ERROR_MESSAGES[err?.code] || err?.message || err}`;
    }
    this._render();
  }

  _userOptions() {
    const options = (this._users || []).map((u) => ({
      value: u.user_id,
      label: u.is_self ? `${u.name} (account owner)` : u.name,
    }));
    // Keep configured ids that aren't visible any more, so they can be removed.
    const known = new Set(options.map((o) => o.value));
    for (const id of [...(this._config.users || []), ...(this._config.hidden_users || [])]) {
      if (!known.has(id)) {
        options.push({ value: id, label: `${id} (not currently shared)` });
        known.add(id);
      }
    }
    return options;
  }

  _colorPeople() {
    // Everyone we can name, plus ids that only exist in the colour config.
    const people = (this._users || []).map((u) => ({ id: u.user_id, name: u.name, color: u.color }));
    const known = new Set(people.map((p) => p.id));
    for (const id of Object.keys(this._config.colors || {})) {
      if (!known.has(id)) people.push({ id, name: `${id} (not currently shared)`, color: this._config.colors[id] });
    }
    return people;
  }

  _schema() {
    const userSelect = {
      select: { multiple: true, mode: "list", custom_value: true, options: this._userOptions() },
    };
    return [
      { name: "title", selector: { text: {} } },
      {
        name: "default_range",
        selector: {
          select: {
            mode: "dropdown",
            options: Object.entries(RANGE_LABELS).map(([value, label]) => ({ value, label })),
          },
        },
      },
      ...(this._data().default_range === "last_n_days"
        ? [{ name: "days", selector: { number: { min: 1, max: MAX_DAYS, step: 1, mode: "box" } } }]
        : []),
      {
        type: "expandable", name: "", title: "People", icon: "mdi:account-multiple", flatten: true,
        schema: [
          { name: "users", selector: userSelect },
          { name: "hidden_users", selector: userSelect },
        ],
      },
      ...(this._colorPeople().length
        ? [{
            type: "expandable", name: "", title: "Colours", icon: "mdi:palette", flatten: true,
            schema: this._colorPeople().map((p, i) => ({
              name: `color:${p.id}`, label: p.name, selector: { color_rgb: {} },
              ...(i === 0 ? { helper: HELPERS.colors } : {}),
            })),
          }]
        : []),
      {
        type: "expandable", name: "", title: "Layout", icon: "mdi:view-dashboard-outline", flatten: true,
        schema: [
          {
            name: "layout",
            selector: { select: { mode: "dropdown",
              options: LAYOUTS.map((value) => ({ value, label: LAYOUT_LABELS[value] })) } },
          },
          {
            name: "sections",
            selector: { select: { multiple: true, reorder: true,
              options: SECTIONS.map((value) => ({ value, label: SECTION_LABELS[value] })) } },
          },
        ],
      },
      {
        type: "expandable", name: "", title: "Map", icon: "mdi:map", flatten: true,
        schema: [
          { name: "map_height", selector: { number: { min: 150, max: 1000, step: 10, mode: "box", unit_of_measurement: "px" } } },
          { name: "show_current", selector: { boolean: {} } },
          {
            name: "pulse",
            selector: { select: { mode: "dropdown", options: [
              { value: "auto", label: "Auto" }, { value: "always", label: "Always" }, { value: "off", label: "Off" }] } },
          },
          { name: "refresh_interval", selector: { number: { min: 0, max: 3600, step: 10, mode: "box", unit_of_measurement: "s" } } },
          { name: "tile_url", selector: { text: {} } },
          { name: "tile_attribution", selector: { text: {} } },
        ],
      },
      {
        type: "expandable", name: "", title: "Advanced", icon: "mdi:cog", flatten: true,
        schema: [{ name: "entry_id", selector: { config_entry: { integration: "geopulse" } } }],
      },
    ];
  }

  _render() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      this._note = document.createElement("div");
      this._note.style.cssText = "color:var(--secondary-text-color);font-size:14px;margin-bottom:8px";
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => schema.label ?? LABELS[schema.name] ?? schema.title ?? schema.name;
      this._form.computeHelper = (schema) => schema.helper ?? HELPERS[schema.name];
      this._form.addEventListener("value-changed", (ev) => this._changed(ev.detail.value));
      this.append(this._note, this._form);
    }
    this._note.textContent = this._users === null ? "Loading GeoPulse users…" : this._usersError || "";
    this._note.hidden = !this._note.textContent;
    this._form.hass = this._hass;
    this._form.schema = this._schema();
    this._form.data = this._data();
  }

  _data() {
    // Effective values, so the form shows defaults; `days` alone = last N days.
    const data = { ...DEFAULTS, ...this._config, sections: sectionsOf(this._config) };
    for (const name of SECTIONS) delete data[`show_${name}`];
    if (this._config.days !== undefined && this._config.default_range === undefined) {
      data.default_range = "last_n_days";
    }
    if (data.default_range === "last_n_days") data.days ??= 3;
    // Each person's colour picker starts at their override or GeoPulse's colour.
    for (const person of this._colorPeople()) {
      data[`color:${person.id}`] = hexToRgb(this._config.colors?.[person.id] || person.color || "#3B82F6");
    }
    delete data.colors;
    return data;
  }

  _changed(value) {
    const config = { ...value };
    // Fold the per-person pickers back into `colors`, keeping only real
    // overrides: picking GeoPulse's own colour again removes the override.
    const geopulse = Object.fromEntries((this._users || []).map((u) => [u.user_id, u.color]));
    const colors = {};
    for (const key of Object.keys(config)) {
      if (!key.startsWith("color:")) continue;
      const id = key.slice(6);
      const hex = Array.isArray(config[key]) ? rgbToHex(config[key]) : null;
      if (hex && hex !== (geopulse[id] || "").toUpperCase()) colors[id] = hex;
      delete config[key];
    }
    if (Object.keys(colors).length) config.colors = colors;
    else delete config.colors;
    // Keep the saved YAML minimal: drop defaults and empty values.
    for (const name of SECTIONS) delete config[`show_${name}`]; // superseded by `sections`
    for (const [key, def] of Object.entries(DEFAULTS)) if (config[key] === def) delete config[key];
    if (JSON.stringify(config.sections) === JSON.stringify(SECTIONS)) delete config.sections;
    for (const key of ["title", "tile_url", "tile_attribution", "entry_id"]) if (!config[key]) delete config[key];
    for (const key of ["users", "hidden_users"]) if (!config[key]?.length) delete config[key];
    if (value.default_range !== "last_n_days") delete config.days;
    const entryChanged = (config.entry_id || null) !== (this._config.entry_id || null);
    this._config = config;
    if (entryChanged) this._loadUsers();
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }
}

if (!customElements.get("geopulse-card-editor")) {
  customElements.define("geopulse-card-editor", GeoPulseCardEditor);
}

if (!customElements.get("geopulse-card")) {
  customElements.define("geopulse-card", GeoPulseCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "geopulse-card",
    name: "GeoPulse timeline",
    description: "Map and stay/trip list from GeoPulse for a date range.",
    preview: false,
    documentationURL: "https://github.com/blauvster/ha-geopulse#timeline-card",
  });
  console.info(`%c GEOPULSE-CARD %c ${VERSION} `, "background:#0f766e;color:#fff", "");
}
