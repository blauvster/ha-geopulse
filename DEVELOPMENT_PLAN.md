# GeoPulse ⇄ Home Assistant — Development Plan

Engineering breakdown for implementing [Plan.md](Plan.md). Each phase
should leave the repo in a working, testable state. Checkboxes track
progress across sessions.

---

## Phase 0 — Repo scaffolding

- [x] `git init`, `.gitignore` (Python + HA custom component conventions)
- [x] Directory layout:
  ```
  custom_components/geopulse/
    __init__.py
    manifest.json
    const.py
    api.py
    config_flow.py
    coordinator.py
    device_tracker.py
    export.py
    diagnostics.py
  www/geopulse-card.js
  hacs.json
  tests/
  README.md
  requirements-test.txt
  ```
- [x] `manifest.json` (domain `geopulse`, `config_flow: true`, `iot_class:
      local_polling`)
- [x] `hacs.json`
- [x] `const.py` seeded with the confirmed `GpsSourceType` enum values
      and endpoint paths from Plan.md §12

## Phase 1 — GeoPulse API client (`api.py`) — done

Thin async `aiohttp`-based wrapper around the endpoints confirmed in
Plan.md §12. `api.py` itself has no HA imports (the package `__init__.py`
does, since Phase 3; tests always run with HA installed).

- [x] `GeoPulseClient(session, base_url, token)` — one client per token;
      construct two instances (read token, export token) rather than a
      single client juggling both
- [x] `async_get_source_configs() -> list[GpsSourceConfig]`
- [x] `async_get_last_known_position() -> GpsPoint | None`
- [x] `async_get_latest_point_for_source_type(source_type) -> GpsPoint | None`
- [x] `async_get_friends() -> list[Friend]`
- [x] `async_get_friend_location(friend_id) -> FriendPoint | None` and
      `async_get_friends_location_trails(minutes, end_time) -> dict[str,
      list[FriendPoint]]` — richer per-friend data (accuracy/altitude/
      velocity, real rolling-window history up to 24h) than the
      `/api/friends` summary alone; see Plan.md §3/§12
- [x] `async_post_homeassistant_location(...)`
- [x] `GeoPulseAuthError` on 401 (→ `ConfigEntryAuthFailed` upstream) and
      `GeoPulseApiError` on other failures
- [x] Unit tests (`tests/test_api.py`, 13 passing) against a real
      `aiohttp.web` test server rather than `aioresponses` — that library
      (0.7.9, its latest release) monkeypatches `ClientResponse` in a way
      that's broken against aiohttp >=3.10's constructor signature, which
      is what installs on this machine's Python 3.14. A tiny real HTTP
      server sidesteps the version coupling entirely and was barely more
      code.

**Test environment:** development moved to a Debian LXC; tests run in
Docker via `docker/run-tests.sh` (image: `docker/Dockerfile.test`,
Python 3.14, `homeassistant` 2026.9.4, `pytest-homeassistant-custom-component`
0.13.367). This replaces the old Windows setup, where the HA plugin couldn't
load (`fcntl` is POSIX-only). Enabling the plugin surfaced two things:
- The plugin blocks sockets, so `test_api.py` opts in via
  `pytestmark = usefixtures("socket_enabled")` (same as HA core's
  `hass_client`; still limited to 127.0.0.1).
- `manifest.json` declared `config_flow: true` with no `config_flow.py`, so
  HA refused to set up any entry. Fixed in Phase 2; `tests/test_init.py`
  guards it.
- `api.py` let aiohttp's total-timeout `TimeoutError` (not a
  `ClientError`) escape as an unhandled exception. Now mapped to
  `GeoPulseApiError`, along with undecodable JSON and wrong-shaped payloads.

## Phase 2 — Config flow — done

- [x] Step 1 (Connection): base URL + read token, validated with live
      `get_source_configs()` + `get_friends()` calls. URL is normalized
      (trailing slash/query dropped, http/https only).
- [x] Step 2 (Import selection): whole-account toggle (default on),
      source types present on the account (excluding `HOME_ASSISTANT`,
      deduped), friends with `friendSharesLiveLocation`. The collapse
      limitation is stated in the step description. Empty pickers are hidden.
- [x] Step 3 (Export selection): `device_tracker` entities, excluding every
      entity registered under the `geopulse` platform; optional HA
      Location Source token (required only if something is selected)
- [x] Step 4 (Export device IDs): one field per exported entity, defaulting
      to the entity_id; empty/duplicate IDs rejected
- [x] Options flow (`OptionsFlowWithReload`, menu): import selection,
      export selection (blank token keeps the stored one; a token-only
      change schedules a reload itself, since options are unchanged),
      settings (poll interval 10–3600s, recorder exclusion)
- [x] Reauth flow (`reauth_confirm`); aborts `wrong_account` if the new
      token belongs to a different account
- [x] Config flow tests: 28 in `tests/test_config_flow.py`, including one
      driving the flow through HA's HTTP API (the frontend's path) so
      selector serialization is covered

**Live server check (2026-09-29):** `docker/probe.sh` runs a read-only,
redacted probe against the server in `.env.local` (git-ignored). It found
`API_PATH_GPS_POINTS` was wrong (`/api/gps/points` → 405; the list is
`GET /api/gps`), and that an unrecognised `sourceTypes` value makes the
server return unfiltered points, which `api.py` now guards against.
Everything else in Plan.md §12 matched, including `userId` on source
configs and friends (the unique-ID source).

**Decisions made here:**
- `entry.data` holds `base_url`, `read_token`, `export_token`;
  `entry.options` holds all selections/settings. `export_entities` is
  `{entity_id: device_id}`.
- Unique ID is the account's `userId`. There's no "who am I" endpoint on
  the API-token surface, but every source config and friendship row
  carries it. An account with neither falls back to the base URL; reauth
  upgrades such an entry to the real `userId` once it's visible.
- The retry queue is one per-entry toggle (default on), not per entity as
  Plan.md §3 suggests; per-entity added UI with no clear use case. Easy to
  split later since it's only read in Phase 4.
- Recorder exclusion is one per-entry default applied at entity creation
  (Phase 3); per-entity overrides use HA's standard entity setting (§6).
- The export token can't be validated at setup: the only way to check it
  is to POST a real point to `/api/homeassistant`.

## Phase 3 — Import (coordinator + device_tracker) — done

Current position only — never history/trails. GeoPulse stays the sole
source of truth for location history; HA only ever reflects "where is
this entity right now" (Plan.md §4). Concretely, the coordinator only
ever calls `async_get_last_known_position`,
`async_get_latest_point_for_source_type`, and `async_get_friends` — never
`async_get_friend_location` or `async_get_friends_location_trails` (those
two are Phase 5/card-only, already flagged as such in `api.py`'s
docstrings).

- [x] `GeoPulseCoordinator` (`TimestampDataUpdateCoordinator`): per poll,
      `asyncio.gather` of only the selected targets — last-known position,
      one call per source type, one `/api/friends` for all friends. Auth
      errors → `ConfigEntryAuthFailed` (reauth), others → `UpdateFailed`.
      First refresh failing → setup retry.
- [x] `device_tracker` entities: `device_tracker.geopulse_account`,
      `geopulse_<source type>`, `geopulse_<friend name>`. Unique IDs are
      `<entry_id>_account|source_<type>|friend_<id>`. Attributes: battery,
      gps_accuracy, altitude, speed, last_seen, geopulse_source (only
      when non-null). Battery is a plain attribute — `battery_level` on
      trackers is deprecated (removed in HA 2027.7). Deselected targets are
      removed from the entity registry on reload.
- [x] Recorder exclusion — **the planned mechanism doesn't exist in HA
      core**; replaced by `_unrecorded_attributes`. See Plan.md §6.
      Verified against a real recorder DB (`tests/test_recorder.py`) and a
      live HA instance.
- [x] Unavailable vs last-known: last-known for max(5 min, 3 polls), then
      unavailable (Plan.md §9). Needed an entity-side timer: the
      coordinator only notifies listeners on the *first* failure of a run,
      so without it `available` was never re-checked and a stale position
      would have shown forever.

**End-to-end check (2026-09-29):** `docker/compose.dev.yml` runs the
official HA 2026.9.4 image with the integration mounted read-only. Config
flow driven over HA's REST API against production GeoPulse created all
four trackers (account, Colota, two friends) and loaded cleanly; history
API confirmed no coordinates recorded. Dev HA state and credentials live
in `.ha-config/` (git-ignored); `docker/ha-token.sh` prints a fresh access
token from the saved refresh token (access tokens expire after 30 min).

## Phase 4 — Export — done

- [x] `GeoPulseExporter` (`export.py`): `async_track_state_change_event` on
      the selected entities; each move (lat/lng/accuracy changed, state not
      unavailable/unknown) becomes a queue item. Router/zone-only trackers
      without coordinates are skipped.
- [x] POST to `/api/homeassistant` with one client per location-source
      token; timestamp is the state's `last_updated` as UTC
      `...sssZ`; battery from `battery_level` (or `battery`).
- [x] Queue persisted with `Store` (`geopulse.<entry_id>.export_queue`,
      delayed save + save on unload), delivered strictly in order,
      at-least-once. Failures back off 30 s → 15 min; new points wait
      behind a pending retry. Capped at 5000 (oldest dropped). With the
      retry toggle off, failed points are dropped. Queue removed with the
      entry.
- [x] Rejected location-source token → a Repairs issue per account (the read token's reauth
      flow isn't involved); cleared on the next successful send.
- [x] `device_id` per entity from the config/options flow (Phase 2).
- [x] Setup no longer fails when the first import poll fails: that would
      have stopped the exporter during exactly the outage the queue is for.
      Trackers start unavailable and recover on the next poll.
- [x] Tests: 22 in `tests/test_export.py`; queue behaviours mutation-checked.

- [x] **Per-tracker accounts (added 2026-09-29):** GeoPulse keeps one
      timeline per user and ignores `device_id` for grouping, so each
      person needs their own GeoPulse account. The device-ID step is one
      screen per tracker with `device_id` + a **required** location-source
      token (`entry.data[export_entity_tokens]`, `{entity_id: token}`; in
      options, blank keeps the stored one). No entry-wide default token —
      removed after review, since it invites mixing people into one
      account. First tried form sections per tracker; dropped because the
      frontend can't translate dynamic section keys and showed raw
      `device_id` / `token` labels. The
      exporter runs one lane per token; queue items carry `entity_id` so
      they're re-routed to the right account after a restart (tokens are
      never written to the queue file), and points for trackers no longer
      exported are dropped. Account setup and friend sharing stay manual
      (admin-API automation rejected — see Plan.md §2).

**Live findings (2026-09-29, one test point, `device_id` `ha-geopulse-test`):**
- Success is an empty `200` with no Content-Type. `response.json()`
  rejected that, so every successful export would have been reported as a
  failure; `api.py` now parses the raw body.
- Null `altitude` or missing `battery` → `500` (server-side unboxing),
  nothing stored. Both are now always sent (unknown → `0.0` / `0`).
- `battery.level` is an int. GeoPulse stores speed as km/h, so the import
  side now converts `velocity` back to m/s for HA.

**Known limitation:** `battery_level` on trackers goes away in HA 2027.7.
After that most trackers will report no battery (→ `0` in GeoPulse) unless
we add an optional per-entity battery sensor mapping.

## Phase 5 — Lovelace card — done

- [x] Research: `GET /api/streaming-timeline/multi-user` returns stays,
      trips, gaps *and* path segments for the owner plus every friend
      sharing their timeline in one call (Plan.md §12); live-verified.
- [x] Backend proxy (`timeline.py`): websocket `geopulse/timeline`
      (+ `geopulse/users` for the editor), whole days in HA's time zone,
      ≤31 days, trimmed payload (~13× smaller), optional `user_ids`
      filter (applied in HA: GeoPulse's own `userIds` 403s the whole
      request if one listed friend stopped sharing). Access: *Timeline card
      viewers* option — empty = every logged-in user, else those users +
      admins (was admin-only). Errors: `invalid_range`, `not_found`,
      `unauthorized` (HA permission), `geopulse_auth_failed`,
      `geopulse_unavailable`.
- [x] Visual editor (`geopulse-card-editor`, HA `ha-form`): people to show
      and hidden by default (names from `geopulse/users`), default range
      (today / yesterday / last 7 / last 30 / last N days), section toggles
      (filters, map, list), map height and tiles, entry. Saves minimal YAML.
- [x] Layout: `sections` (reorderable: which parts, in order) + `layout`
      (auto/stacked/columns); fills panel views and sized sections-view
      cells. Checked in headless Chromium at narrow/wide/panel sizes (a
      stand-in `ha-card` with inline `display:block` first made the fill
      look broken — harness bug, not card).
- [x] "Now" markers per person when the range includes today, plus
      auto-refresh; colours hex-validated server-side. First render showed
      hollow rings — a `.dot` class clash with the people chips.
- [x] Per-person colour overrides (`colors`) with editor pickers, and
      grouped "now" markers for people at the same spot. Fixed along the
      way: now-markers had `position: relative`, overriding Leaflet's
      absolute positioning — every marker after the first (friends) was
      pushed 14 px down, which looked like zoom-dependent drift (measured
      before/after). Full-screen panels now measure their height (HA's
      `height: 100%` chain isn't definite there), so the list scrolls.
- [x] OSM tiles send the origin as Referer (`referrerPolicy: origin`):
      HA pages are `Referrer-Policy: no-referrer`, and OSM serves "Access
      blocked" tiles without one — reported from the real dashboard;
      headless Chromium didn't reproduce it (OSM treats it differently).
- [x] Card (`frontend/geopulse-card.js`, no build step): Today /
      Yesterday / 7 days, date pickers with prev/next, per-person toggle
      chips with distance, map (paths + stay markers with tooltips, fit to
      data), chronological list (stays, trips with movement icon,
      distance and duration, data gaps; day headers for ranges; click to
      focus the map). Light/dark, metric/imperial, HA time zone and locale.
- [x] Served by the integration with vendored Leaflet 1.9.4 (npm tarball,
      integrity-checked) and auto-registered on all dashboards — HACS
      can't ship an integration and a dashboard plugin from one repo.
- [x] Tests: `tests/test_timeline.py` (trimming, day boundaries in HA's
      tz, admin-only, range limits, error mapping, entry selection, card
      registration). Visual check in headless Chromium with fake data
      (light, dark, error) — found CARTO tiles now need an API key, so the
      default is OpenStreetMap. Live check against production through the
      dev HA: 3 people, 29 KiB/day, 7 days in 0.4 s.

## Phase 6 — Packaging & docs — done

- [x] README for users: install (HACS / manual), setup, the one-account-
      per-person export model, card config, privacy/recorder, behaviour,
      known limitations, troubleshooting, development
- [x] `hacs.json` for an integration repo (dropped the plugin-style
      `filename`; `homeassistant: 2026.9.0`, the version tested against)
- [x] `hassfest` (official Docker image) passes with no warnings
- [x] CI (`.github/workflows/ci.yml`): hassfest, HACS validation (brands
      check ignored — images ship in `brand/`), pytest on Python 3.14;
      weekly run to catch new HA/HACS releases
- [x] Versioning: SemVer; `manifest.json` is the source of truth, the card
      declares the same version (test keeps them in sync); GitHub releases
      tagged `vX.Y.Z`. `CHANGELOG.md`.
- [x] License: MIT (Leaflet's BSD-2 licence kept with the vendored copy)
- [x] Diagnostics (`diagnostics.py`): entry data redacted, no tokens,
      server URL or coordinates — test asserts none leak
- [x] `requirements-test.txt` pinned (plugin 0.13.367 → HA 2026.9.4)
- [x] Brand icons in `custom_components/geopulse/brand/` (`icon`, `dark_icon`,
      each at 256 px and `@2x` 512 px). HA 2026.9 serves a custom
      integration's `brand/` folder locally before the brands CDN; `logo*`
      fall back to the icons. Original artwork (teal pin with a pulse
      line), deliberately not derived from GeoPulse's BSL-licensed logo.
      Sources: `assets/icon.svg`, `assets/icon-dark.svg`; re-render with
      `rsvg-convert -w 256 -h 256` (and 512 for `@2x`).
- [ ] Optional: ask the GeoPulse maintainer about using the official logo
      instead, once the integration is published.
- [ ] Screenshots for the README (need a real dashboard; left for the
      maintainer so no real location data is published by accident)
- [ ] First GitHub release `v0.1.0` once the repo is public and CI is green

---

## Current status

All phases done; 112 tests passing in Docker (`docker/run-tests.sh`),
hassfest clean. Import, export (two accounts) and the card are verified
live against a production GeoPulse. Remaining: README screenshots and the
first release.
