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
- [x] Unit tests (`tests/test_api.py`, 10 passing) against a real
      `aiohttp.web` test server rather than `aioresponses` — that library
      (0.7.9, its latest release) monkeypatches `ClientResponse` in a way
      that's broken against aiohttp >=3.10's constructor signature, which
      is what installs on this machine's Python 3.14. A tiny real HTTP
      server sidesteps the version coupling entirely and was barely more
      code.

**Windows testing note:** `pytest-homeassistant-custom-component` (and
`homeassistant` itself, via `homeassistant.runner`) imports the stdlib
`fcntl` module at import time, which doesn't exist on Windows — it's
POSIX-only. This means Phase 2 onward, once tests depend on that plugin
for config-flow/coordinator/entity fixtures, **this dev machine can't run
them natively**. `api.py`'s own tests (Phase 1, pure `aiohttp`, no HA
import) are unaffected and pass locally. Options once Phase 2 needs real
HA test fixtures: run pytest inside WSL2, a Linux devcontainer, or CI
(GitHub Actions `ubuntu-latest`) — this is a common constraint for HA
custom-component development on Windows, not specific to this project.

## Phase 2 — Config flow

- [ ] Step 1 (Connection): base URL + read token, validated with a live
      `get_source_configs()` call
- [ ] Step 2 (Import selection): list source *types* present on the
      account (excluding `HOME_ASSISTANT`) + friends with
      `friendSharesLiveLocation`; per Plan.md §3, multiple devices
      sharing one source type collapse into a single entity — surface
      that in the step's UI copy so it isn't a silent surprise
- [ ] Step 3 (Export selection): existing `device_tracker` entities not
      owned by this config entry; optional HA Location Source token
      (can be added later via Options)
- [ ] Options flow: add/remove import or export entities, poll interval,
      recorder-exclusion toggle
- [ ] Reauth flow on `GeoPulseAuthError`
- [ ] Config flow tests (`pytest-homeassistant-custom-component`)

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

Starting Phase 0 now.
