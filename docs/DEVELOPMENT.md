# Development notes

How the integration works, why it's built the way it is, and what we know
about GeoPulse's API. For users, see the [README](../README.md).

## Architecture

```
custom_components/geopulse/
  api.py            GeoPulse REST client (no HA imports)
  config_flow.py    setup, options (import / export / settings) and reauth
  coordinator.py    import polling (current positions only)
  device_tracker.py imported trackers
  export.py         HA trackers -> GeoPulse, persistent per-account queues
  timeline.py       card backend: websocket proxy + card registration
  diagnostics.py    redacted diagnostics
  frontend/         the Lovelace card (plain JS) + vendored Leaflet 1.9.4
  brand/            icons HA serves for the integration
```

- `entry.data`: `base_url`, `read_token` (GeoPulse API token) and
  `export_entity_tokens` (`{entity_id: location-source token}`).
- `entry.options`: import selections, `export_entities`
  (`{entity_id: device_id}`), `export_retry_queue`, `poll_interval`,
  `recorder_exclude`, `timeline_users`.
- Entries are keyed by the GeoPulse account's `userId` (read from source
  configs / friendships — there's no "who am I" endpoint for API tokens),
  falling back to the base URL for an account with neither; reauth adopts
  the real id once visible.

## Tokens

Two kinds of GeoPulse credential:

- **API token** (one per entry, "read token"): full access to the account —
  GeoPulse has no read-only tokens. Used for import and the card.
- **Location-source token** (one per exported tracker): a GeoPulse
  *Home Assistant* location source. It can only add points to that
  account.

GeoPulse builds **one timeline per user**; `device_id` is stored on points
but only as a label (CSV/GeoJSON export, status panel) — timeline, map and
friends ignore it. So trackers for different people must go to different
GeoPulse accounts, which is why the token is per tracker with no
entry-wide default. Account creation and friend sharing are left to the
user; automating them via the admin API was rejected (no admin "create
user" or "create source for user" endpoint, and it would mean handling an
admin token with full server control).

## Import

- The coordinator polls **current position only** — last-known position,
  one latest point per selected source type, and `/api/friends` for all
  friends in one call. It never calls trail/history endpoints: history
  stays in GeoPulse.
- Source types come from the account's source configs, minus
  `HOME_ASSISTANT` (that's our own export — importing it would loop). Two
  devices on the same source type are indistinguishable in the API and
  collapse into one tracker.
- Friends are offered only if they share their *live location*.
- **GeoPulse unreachable:** trackers keep their last position for
  max(5 min, 3 polls), then go unavailable — short outages don't fire
  "left home" automations. The coordinator only notifies listeners on the
  *first* failure, so each tracker sets its own grace-expiry timer.
- Setup doesn't fail when the first poll fails: that would also stop the
  exporter during exactly the outage its queue is for. Trackers start
  unavailable and recover.
- GeoPulse stores speed as km/h; trackers expose m/s.
- `battery_level` on trackers is deprecated (HA 2027.7), so battery is a
  plain `battery` attribute.

## Recorder

HA core has no per-entity "exclude from recorder" registry option — the
recorder only honours its YAML filter; `recorder/entity_options` only
*reports* it. So imported trackers use `_unrecorded_attributes` (latitude,
longitude, gps_accuracy, altitude, speed, last_seen). Zone state and
battery are still recorded, which keeps presence history and the logbook.
It's class-level, hence two tracker classes selected by the
`recorder_exclude` option. Verified against a real recorder DB
(`tests/test_recorder.py`).

## Export

- `async_track_state_change_event` on the selected trackers; a point is
  queued only when latitude/longitude/accuracy change and the state has
  coordinates.
- **One lane per token** (= per GeoPulse account): its own queue,
  30 s → 15 min backoff and Repairs issue, so one account's problem doesn't
  hold up the others. Lanes share one `Store`
  (`geopulse.<entry_id>.export_queue`); items carry `entity_id`, never the
  token, and are re-routed after a restart (points for trackers no longer
  exported are dropped). Delivery is in order, at-least-once; up to 5000
  points per lane. With the retry toggle off, failed points are dropped.
- Loop avoidance is structural: the export picker excludes every entity of
  this integration; friends are imported only if chosen.

## Timeline card

- **Proxy:** websocket `geopulse/timeline {start_date, end_date,
  entry_id?, user_ids?}` and `geopulse/users`. The API token stays in HA —
  verified: none of an entry's tokens appear in anything the browser can
  fetch. Dates are whole days in HA's time zone, ≤31 days; the payload is
  trimmed ~13× to what the card draws.
- **Access:** `timeline_users` — empty means every logged-in HA user,
  otherwise those users plus administrators. Whoever can use the card sees
  everything the API token can.
- `user_ids` is filtered in HA rather than via GeoPulse's `userIds`: with
  that, one listed friend who stopped sharing makes GeoPulse reject the
  whole request.
- **"Now" markers:** for ranges reaching the present, the proxy also
  fetches the owner's last-known position and `/api/friends` (live-location
  sharers), best effort. Markers within 20 px are grouped; the card
  auto-refreshes (default 120 s) without refitting the map.
- **Colours:** GeoPulse assigns them from a 10-colour palette by position
  in its people list (sharing friends, then the owner), so they shift as
  friends come and go; `colors` overrides them. Colours are hex-validated
  server- and client-side because they end up in styles/markup. All other
  data is rendered as text, never HTML.
- **Layout:** `sections` (which parts, in order) + `layout` (auto /
  stacked / columns, auto from 700 px). HA sets `layout = "panel"` or
  `"grid"` on cards; in a panel the card measures its height (HA's
  `height: 100%` chain isn't definite there), in a grid cell with explicit
  rows it uses `height: 100%`.
- **Tiles:** OpenStreetMap raster tiles. CARTO basemaps now need an API
  key; HA's own map moved to vector tiles (too heavy to vendor). HA pages
  send `Referrer-Policy: no-referrer` and OSM blocks tile requests without
  a Referer, so OSM tiles use `referrerPolicy: "origin"`.
- **Delivery:** HACS can't install one repo as both an integration and a
  dashboard plugin, so the integration serves the card and registers it on
  every dashboard (`add_extra_js_url`, cache-busted by content hash).

## GeoPulse API

Verified against the backend source (`tess1o/geopulse`) and live against a
production server with `docker/probe.sh` (2026-09). API-token endpoints
accept `Authorization: Bearer <token>` or `X-API-Key` and return
`{"status", "message", "data"}` unless noted.

| Endpoint | Notes |
|---|---|
| `GET /api/gps/source/` | **Raw list** (no envelope) of source configs: `id, type, deviceId, userId, active, …`. |
| `GET /api/gps/last-known-position` | One `GpsPointDTO` or `null`, across all sources. |
| `GET /api/gps?sourceTypes=T&limit=1&sortBy=timestamp&sortOrder=desc` | Paginated points: `data.data[]` + `data.pagination`. Not `/api/gps/points` (POST-only, 405 on GET). An **unknown `sourceTypes` value is silently ignored** (unfiltered result), so the client checks the returned `sourceType`. |
| `GET /api/friends` | `FriendInfoDTO[]`: `userId` (you), `friendId`, `fullName`, `email`, `lastLatitude/Longitude`, `lastSeen`, `lastBattery`, `friendSharesLiveLocation`, `friendSharesTimeline`. |
| `GET /api/streaming-timeline/multi-user?startTime&endTime[&userIds]` | Owner + friends sharing their timeline: `timelines[] {userId, fullName, email, assignedColor, timeline {stays, trips, dataGaps}, pathSegments, stats}`. Durations in seconds, distances in metres. A listed friend without permission → 403 for the whole request; the owner is always allowed. |
| `POST /api/homeassistant` | Location-source token as Bearer. Body: `device_id`, `timestamp` (ISO instant), `location {latitude, longitude, accuracy, altitude, speed (m/s)}`, `battery {level (int)}`. Success: **empty 200 with no Content-Type** (aiohttp's `json()` rejects it — the client parses the raw body). Bad token: 401. **Null `altitude` or missing `battery` → 500** (server-side unboxing), so both are always sent (unknown → `0.0` / `0`). |

- `GpsPointDTO`: `id, timestamp, coordinates {lat, lng}, accuracy, battery,
  velocity (km/h), altitude, sourceType`. No device id.
- `GpsSourceType`: `OWNTRACKS, GPSLOGGER, OVERLAND, TRACCAR,
  GOOGLE_TIMELINE, GPX, DAWARICH, HOME_ASSISTANT, GEOJSON, CSV, COLOTA,
  MANUAL, MOBILE_APP`.
- Trip `movementType`: `WALK, RUNNING, BICYCLE, CAR, MOTORCYCLE, TRAIN,
  FLIGHT, BOAT, UNKNOWN`.
- Worth reporting upstream: the 500 on missing altitude/battery, and the
  silently ignored `sourceTypes` value.

## Development and testing

Tests need a POSIX host (HA's test plugin imports `fcntl`) and run in
Docker:

```sh
docker/run-tests.sh                 # full suite; extra args go to pytest
docker/run-tests.sh tests/test_export.py -k lane
```

- `docker/compose.dev.yml`: a dev Home Assistant (2026.9.4) with the
  integration mounted read-only; state in `.ha-config/` (git-ignored).
  `docker/ha-token.sh` prints a fresh access token (they expire after
  30 min). Note: the frontend answers before integrations finish loading,
  so wait for the card URL to appear in `/` before checking it.
- `docker/probe.sh`: read-only, redacted check of a real GeoPulse server
  against the table above, using `GEOPULSE_URL` / `GEOPULSE_READ_TOKEN`
  from `.env.local` (git-ignored; passed via `--env-file`, since tokens can
  contain shell metacharacters).
- The card has no build step. Check it in headless Chromium with fake data
  (never real locations in screenshots); headless Chromium doesn't
  reproduce OSM's referrer blocking, so test tiles in a real browser too.
- Versions: SemVer; `manifest.json` is the source of truth, the card's
  `VERSION` must match (tested). Releases are GitHub releases `vX.Y.Z`.
- CI (`.github/workflows/ci.yml`): hassfest, HACS validation, pytest.
