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
Plan.md §12. No HA-specific code here — independently testable (verified:
`custom_components/geopulse/__init__.py` only imports `homeassistant`
inside its functions, so `api.py` never pulls in HA at import time).

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

## Phase 3 — Import (coordinator + device_tracker)

Current position only — never history/trails. GeoPulse stays the sole
source of truth for location history; HA only ever reflects "where is
this entity right now" (Plan.md §4). Concretely, the coordinator only
ever calls `async_get_last_known_position`,
`async_get_latest_point_for_source_type`, and `async_get_friends` — never
`async_get_friend_location` or `async_get_friends_location_trails` (those
two are Phase 5/card-only, already flagged as such in `api.py`'s
docstrings).

- [ ] `DataUpdateCoordinator` polling per Plan.md §4, one fetch per
      configured import target per cycle
- [ ] `device_tracker` entities: per-source-type, whole-account-aggregated,
      and per-friend, per Plan.md §3/§4
- [ ] Recorder exclusion on entity creation is **required, not
      best-effort** (Plan.md §6 — corrected from an earlier draft that
      wrongly assumed this was already the HA core default) — verify the
      `async_update_entity_options` signature against the targeted HA
      core version first (flagged as unverified in Plan.md §9), and treat
      it as a blocking part of entity setup rather than a nice-to-have
- [ ] Decide + implement unavailable-vs-last-known behavior when
      GeoPulse is unreachable (open item in Plan.md §9)

## Phase 4 — Export

- [ ] `async_track_state_change_event` per selected entity_id
- [ ] POST to `/api/homeassistant` with the payload shape from Plan.md §12
- [ ] Retry queue persisted via HA's `Store` helper (survives restarts,
      per the earlier plan review decision)
- [ ] `device_id` field defaults to the HA `entity_id`, editable at setup

## Phase 5 — Lovelace card

This is the only phase that touches location *history* — `api.py`
already has `async_get_friend_location` and
`async_get_friends_location_trails` ready for a friend's path; the
account's own history still needs research below.

- [ ] Research `/api/streaming-timeline` and related endpoints (deferred
      from Plan.md §12 — not yet pulled from source)
- [ ] Backend proxy (HA websocket command or `/api/geopulse/...` REST
      endpoint) so the raw API token never reaches frontend JS
- [ ] `www/geopulse-card.js`: map + stay/trip list for a selected date
      range

## Phase 6 — Packaging & docs

- [ ] README with setup instructions, screenshots
- [ ] `hacs.json` validation, versioning scheme
- [ ] License

---

## Current status

Phases 0–2 done; 42 tests passing in Docker (`docker/run-tests.sh`).
Next: Phase 3 (coordinator + device_tracker).
