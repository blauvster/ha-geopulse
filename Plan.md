# GeoPulse ⇄ Home Assistant Integration — Project Plan

## 1. Overview

A custom Home Assistant integration (distributed via HACS) that bridges
[GeoPulse](https://github.com/tess1o/geopulse) (a self-hosted, privacy-first
location timeline platform) with Home Assistant, in both directions:

- **Import:** bring selected GeoPulse users/devices into HA as
  `device_tracker` entities.
- **Export:** push selected existing HA `device_tracker` entities out to
  GeoPulse, replacing GeoPulse's documented manual
  `rest_command` + `automations.yaml` approach with native integration code
  (adds retry-on-failure and optional history backfill, which the manual
  method lacks).

Packaged as a single HACS repository containing both the integration
(`custom_components/geopulse/`) and a companion Lovelace card
(`www/geopulse-card.js`) for viewing historical GeoPulse timeline data
(map + stay/trip list) inside HA.

### Why both directions in one integration

GeoPulse's own "Home Assistant" source is push-only and manual: the user
edits `configuration.yaml`/`automations.yaml` per device, with no retry
queue and no backfill (confirmed via GeoPulse's own docs and maintainer
comments in GitHub discussions). Building the export side natively lets us
fix both gaps. Building import and export in the same config flow also
solves the "don't re-import HA-originated data" problem *structurally*: the
export picker only offers device_trackers not already created by this
integration's import side, so the two directions can't overlap by
construction. No runtime "is this from HA?" detection logic is needed.

---

## 2. Authentication

Two separate GeoPulse credentials, both entered during setup:

1. **Read API token** — a GeoPulse user API token, sent as `X-API-Key`
   (or `Authorization: Bearer`), used for polling users/devices/timeline
   data (import direction + the card).
2. **"Home Assistant" Location Source token** — created in GeoPulse under
   *Settings → Location Sources → Add New Source → Home Assistant*. Used
   only for the export direction, POSTing to `/api/homeassistant`. One
   token covers every exported device; GeoPulse tells devices apart via
   the `device_id` field in each payload, not the token.

Handle `401`/expired-token responses with `ConfigEntryAuthFailed` to
trigger HA's reauth flow.

---

## 3. Config Flow

### Step 1 — Connection
- GeoPulse base URL
- Read API token
- (Optional at this point, can be added later via Options) Home Assistant
  Location Source token, for export

### Step 2 — Import selection

GeoPulse has no "users/devices" list API — confirmed against the backend
source (`GpsPointResource`, `GpsSourceConfigResource`, `FriendResource`).
The actual model, and what v1 import is built around:

- **Own account** — `GET /api/gps/source/` lists the token owner's GPS
  source configs (each with a `type`, e.g. `HOME_ASSISTANT`, `OWNTRACKS`,
  `GPSLOGGER`; full enum in §9). Pre-filter out any config whose `type`
  is `HOME_ASSISTANT` (that's data this integration itself exported —
  re-importing it would loop). Present the remaining source types as a
  multi-select. GeoPulse has no per-device-id last-position endpoint —
  `GET /api/gps/points?sourceTypes=<type>&limit=1&sortOrder=desc` is used
  to get the latest point *per selected source type*, so "one entity per
  device" in practice means one entity per distinct GPS source *type* on
  the account (two devices sharing the same source type, e.g. two phones
  both on OwnTracks, aren't distinguishable via this API and will collapse
  into one entity — documented as a known limitation).
  Alternatively, **one aggregated entity for the whole account**
  (`GET /api/gps/last-known-position`, latest point across all source
  types) — configurable at setup, same as before.
- **Friends** — `GET /api/friends` returns the token owner's friends,
  each already carrying `lastLatitude`/`lastLongitude`/`lastBattery`/
  `lastSeen` and a `friendSharesLiveLocation` permission flag. Only
  friends with that flag true are offered; each becomes one aggregated
  device_tracker entity (friends don't expose a per-source-type
  breakdown, only one last-known position each).

### Step 3 — Export selection
- List existing HA `device_tracker` entities **not already created by this
  integration's import side** (structural exclusion — no manual filtering
  needed).
- Multi-select which to export.
- Per selected entity:
  - Retry queue: **on by default**.
  - `device_id` (export payload field): defaults to the HA `entity_id`,
    editable by the user during setup.

### Options Flow
- Add/remove import or export entities after initial setup.
- Adjust poll interval.
- Toggle recorder-exclusion per entity (see §6).

---

## 4. Import Direction (GeoPulse → HA)

- `DataUpdateCoordinator` polling GeoPulse's read API on a configurable
  interval (default ~30–60s). No webhook path exists in GeoPulse today
  (confirmed — it's ingest-only, no outbound webhook feature), so polling
  is the only option for v1. Leave an extension point in the coordinator
  in case GeoPulse adds outbound webhooks later.
- One `device_tracker` entity per configured selection (per-source-type,
  whole-account-aggregated, or friend — see §3), `source_type: gps`. For
  the whole-account-aggregated entity, the source with the most recent
  timestamp across all source types wins on each poll.
- Attributes populated from the confirmed `GpsPointDTO` fields: `battery`,
  `accuracy` (`gps_accuracy`), `speed` (`velocity` in the API), `altitude`.
- Friend entities: current position only, from the single `GET
  /api/friends` call (`lastLatitude`/`lastLongitude`/`lastBattery`/
  `lastSeen` for every friend in one request) — no accuracy/altitude/speed
  attributes for friend entities in v1, since getting those would mean an
  extra per-friend call for no benefit here.
- **History stays in GeoPulse, deliberately.** The coordinator only ever
  fetches *current* position (`last-known-position`, latest-point-per-
  source-type, or the `/api/friends` summary) — never
  `/api/friends/location/trails` or any other history/trail endpoint.
  Combined with recorder exclusion (§6), this means HA never mirrors
  GeoPulse's location history into its own database; GeoPulse remains the
  single source of truth for it, and HA only ever reflects "where is this
  entity right now." The trails endpoint is reserved for the Lovelace
  card (§7), which queries history live and on-demand instead.
- No zone/geofence sync in v1 (GeoPulse "saved places" ↔ HA zones is a
  possible future enhancement, not in scope now).

---

## 5. Export Direction (HA → GeoPulse)

- Replaces the manual `rest_command`/`automations.yaml` pattern from
  GeoPulse's docs with native integration code.
- `async_track_state_change_event` on each selected entity_id.
- On state change, POST to `<geopulse_url>/api/homeassistant` with the
  Home Assistant Location Source token as Bearer auth, payload matching
  GeoPulse's documented schema:

  ```json
  {
    "device_id": "<configurable string, defaults to the HA entity_id>",
    "timestamp": "<ISO8601>",
    "location": {
      "latitude": ...,
      "longitude": ...,
      "accuracy": ...,
      "altitude": ...,
      "speed": ...
    },
    "battery": { "level": ... }
  }
  ```

- Server quirk (§12): altitude and battery must always be present, so
  unknown values are sent as `0.0` altitude and battery level `0`. Worth
  reporting upstream; a `0` battery on a tracker without one is the
  visible side effect.
- **Retry queue** (default on): buffer failed POSTs (GeoPulse unreachable,
  network blip) and retry rather than silently dropping the point, as the
  manual method does today. Persisted to disk (HA `Store` helper) so
  queued points survive an HA restart during an outage, not just
  in-memory buffering.
- **History backfill: out of scope for v1** (see §11). Exported entities
  start fresh from setup time; no replay of pre-existing HA recorder
  history.

---

## 6. Recorder Handling

- **Not excluded by default** — this needed correcting from an earlier
  draft of this plan that assumed device_tracker lat/long attributes were
  already recorder-excluded at the HA core level; that isn't the case, and
  it matters more now that history is deliberately GeoPulse-only (§4) —
  without this, frequent polling would mirror the same data into HA's own
  recorder DB, exactly what we're trying to avoid.
- **Corrected 2026-09-29:** the mechanism an earlier draft named here —
  `entity_registry.async_update_entity_options(entity_id, "recorder",
  {"exclude": True})` — does nothing in HA core (checked against
  2026.9.4). The recorder only honours its YAML `recorder: exclude:`
  filter; the `recorder/entity_options` websocket just *reports* that
  filter, and nothing reads a `recorder` registry option.
- What the integration does instead: imported trackers use
  `_unrecorded_attributes` (the supported per-entity mechanism core
  integrations use) for `latitude`, `longitude`, `gps_accuracy`,
  `altitude`, `speed` and `last_seen`. Coordinates and movement data never
  reach the recorder DB — verified with a real recorder in
  `tests/test_recorder.py` and in a live HA instance against production
  GeoPulse.
- The **zone state** (`home` / `not_home` / zone name), battery and
  metadata *are* still recorded. So HA keeps a coarse presence history,
  which also means Logbook and "arrived home" history keep working. Where
  you were stays GeoPulse-only; whether you were home does not.
- **Default: on.** A per-entry option ("Exclude imported trackers from the
  recorder") switches to a tracker class that records everything.
  `_unrecorded_attributes` is class-level, so this is per entry, not per
  entity.
- For full exclusion including zone state, users can add a YAML filter;
  entity ids are all `device_tracker.geopulse_*`, so
  `recorder: exclude: entity_globs: [device_tracker.geopulse_*]` works.
  Document this in the README.

---

## 7. Custom Lovelace Card

- Renders both an interactive **map** (path/route for a selected date
  range) and a **timeline list** (stays & trips, similar to GeoPulse's own
  timeline view) in one card.
- This is where the history/trail endpoints from §12 that the coordinator
  deliberately never touches actually get used: `/api/friends/location/
  trails` and `/api/friends/{friendId}/location` for a friend's path, and
  `/api/streaming-timeline` (still unresearched, see §9) for the account's
  own stays/trips and presumably its own point-path equivalent.
- Queries GeoPulse's REST API **live**, on demand, for whatever date range
  the user picks — does not rely on HA's recorder/history (consistent
  with excluding these entities from the recorder, per §6).
- Needs a safe path to the GeoPulse API token from frontend JS. Preferred
  approach: the integration exposes a backend proxy (HA websocket API
  command or REST endpoint under `/api/geopulse/...`) that the card calls,
  keeping the raw API token server-side rather than embedded in frontend
  code.

---

## 8. Distribution

- Single HACS custom repository:
  - `custom_components/geopulse/` — the integration
  - `www/geopulse-card.js` — the Lovelace card
  - `hacs.json` declaring both
- License, versioning, README/docs: TBD before first release.

---

## 9. Open Items / Risks

- ~~Confirm whether GeoPulse's read API token can be scoped read-only~~
  **Resolved:** no read-only scoping exists — a user API token grants
  full access to every User-API endpoint that account can reach
  (confirmed via GeoPulse docs). The card's backend proxy holding this
  token is exactly as sensitive as the user's own GeoPulse login; document
  this plainly rather than implying it's a limited-scope credential.
- ~~Pin down exact GeoPulse API endpoints/schemas~~ **Resolved for
  import/export** — see §12 for the confirmed endpoints, verified against
  GeoPulse's backend source (`tess1o/geopulse@main`), not just its docs
  site (whose docs pages and generated OpenAPI spec are both incomplete/
  stale in places). Timeline/stays/trips endpoints for the Lovelace card
  (§7) are still unresearched — do that when starting the card, not
  blocking for the integration itself.
- No documented rate limits found for GeoPulse's API — confirm the chosen
  poll interval is reasonable for a typical self-hosted instance.
- ~~Decide `unavailable` vs. last-known state when GeoPulse is
  unreachable~~ **Resolved:** trackers keep their last position for a
  grace period of max(5 min, 3 poll intervals), then go unavailable.
  Short outages don't cause zone exits (home → unavailable → home) that
  would fire automations; longer ones don't pass a stale position off as
  current.
- ~~Recorder-exclusion API~~ **Resolved — it doesn't work;** see §6 for
  what replaced it.

---

## 12. Confirmed API Reference (v1 scope)

Live-verified 2026-09-29 against a production server with
`scripts/probe_geopulse.py` (read-only; run via `docker/probe.sh`): all rows
below match except the points-list path, now corrected. Originally verified
2026-09-21 directly against `tess1o/geopulse` backend source
(`backend/src/main/java/org/github/tess1o/geopulse/...`), not just docs —
the docs site's own OpenAPI export has empty response schemas for most
GET endpoints. Base path is the GeoPulse server root; all `User API`
endpoints accept `X-API-Key: <token>` or `Authorization: Bearer <token>`
and return the envelope `{"status": "success"|"error", "message": str|null,
"data": T|null}` unless noted.

| Purpose | Method & Path | Notes |
|---|---|---|
| List own GPS source configs | `GET /api/gps/source/` | Returns a **raw** `List<GpsSourceConfigDTO>` (no envelope). Fields: `id, type, username, token, deviceId, hasPayloadEncryptionSecret, userId, active, connectionType, filterInaccurateData, maxAllowedAccuracy, maxAllowedSpeed, enableDuplicateDetection, duplicateDetectionThresholdMinutes`. `type` is one of the `GpsSourceType` enum (below) — filter out `HOME_ASSISTANT` on the import side. |
| Account-wide last known position | `GET /api/gps/last-known-position` | Envelope `data` is a single `GpsPointDTO` or `null`. No device/source scoping — this is the latest point across *all* the account's sources. |
| Latest point for one source type | `GET /api/gps?sourceTypes=<TYPE>&limit=1&sortBy=timestamp&sortOrder=desc` | **Corrected 2026-09-29 against a live server:** the path is `/api/gps`, not `/api/gps/points` (that one is POST-only mobile ingest and returns 405 to GET). `data` is `GpsPointPageDTO` `{data: [GpsPointDTO], pagination: {page, limit, total, totalPages}}`. `GpsPointDTO` fields: `id, timestamp, coordinates{lat,lng}, accuracy, battery, velocity, altitude, sourceType` (plus `telemetryGpsData`, `telemetryCurrentPopup`, unused). No `deviceId` field — can't distinguish two devices sharing one `sourceType`. `sourceTypes` is comma-separated; an unrecognised value is silently ignored server-side (no filter applied), so the client checks the returned `sourceType`. |
| List friends | `GET /api/friends` | `data` is `List<FriendInfoDTO>`: `userId, friendId, avatar, lastLongitude, lastLatitude, fullName, email, lastSeen, lastBattery, lastLocation, latestActivityType, latestActivityDurationSeconds, friendSharesLiveLocation, friendSharesTimeline`. Only offer friends with `friendSharesLiveLocation == true` for import. |
| *(card only, not polled by the coordinator)* One friend's current location | `GET /api/friends/{friendId}/location` | Path param is the friend's user id (UUID). `data` is a `GpsPointPathPointDTO`: `id, longitude, latitude, timestamp, accuracy, altitude, velocity, userId, sourceType`. No battery. |
| *(card only, not polled by the coordinator)* Recent history for all friends | `GET /api/friends/location/trails?minutes=N&endTime=<ISO>` | `N` required, server-validated `1 <= N <= 1440` (max 24h lookback), `endTime` optional (defaults to now). `data` is `{"<friendId>": [GpsPointPathPointDTO, ...], ...}` — a real rolling-window trail per friend. Only covers friends currently sharing live location. Reserved for §7's map card, not the import coordinator — see §4. |
| Export location (Home Assistant source) | `POST /api/homeassistant` | **Public** endpoint (no JWT/API-token auth) — identity comes from `Authorization: Bearer <location-source-token>` alone, verified against `config` matching a `GpsSourceConfigEntity` of type `HOME_ASSISTANT`. Body is `HomeAssistantGpsData`: `device_id` (string), `timestamp` (ISO-8601 `Instant`), `location.{latitude, longitude}` (`double`), `location.{accuracy, altitude, speed}` (`Double`, speed in m/s — stored as km/h), `battery.level` (**`int`**). **Live-verified 2026-09-29:** success is `200` with an empty body and *no* Content-Type; a bad token is `401`; and despite being "optional", a null `altitude` or a missing `battery` object makes the server's mapper throw → `500`, nothing stored. The client always sends both (unknown → `0.0` / `0`). |

`GpsSourceType` enum (for the §3 pre-filter and per-type import):
`OWNTRACKS, GPSLOGGER, OVERLAND, TRACCAR, GOOGLE_TIMELINE, GPX, DAWARICH,
HOME_ASSISTANT, GEOJSON, CSV, COLOTA, MANUAL, MOBILE_APP`.

Not yet researched (defer to card-building phase): `/api/streaming-timeline`
and related stays/trips endpoints for §7's Lovelace card.

---

## 10. Testing

- `pytest-homeassistant-custom-component` for the integration (config
  flow, coordinator, entity creation, export retry queue).
- Manual/visual testing for the Lovelace card.

---

## 11. Out of Scope for v1

- Zone/geofence sync between GeoPulse saved places and HA zones.
- Outbound webhook support (GeoPulse doesn't have this feature yet).
- Multi-instance GeoPulse support (one GeoPulse instance per config
  entry).
- History backfill for exported entities (replaying pre-existing HA
  recorder history into GeoPulse). Dropped from v1 to avoid depending on
  recorder history that may not exist for these entities (see §6); revisit
  as a future enhancement if there's demand.