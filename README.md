# GeoPulse for Home Assistant

<img src="assets/icon.svg" alt="" width="96" align="right">

Connects Home Assistant with [GeoPulse](https://github.com/tess1o/geopulse),
the self-hosted, privacy-first location timeline — in both directions:

- **Import:** GeoPulse positions become `device_tracker` entities in Home
  Assistant: your whole account, individual GeoPulse location sources, and
  friends who share their live location with you.
- **Export:** Home Assistant `device_tracker`s (e.g. the Companion app) are
  sent to GeoPulse, with a retry queue that survives outages and restarts.
  This replaces GeoPulse's manual `rest_command` + automation recipe.
- **Timeline card:** a dashboard card with a map and a list of stays and
  trips for any date range, for you and friends sharing their timeline.

GeoPulse stays the only place your location *history* lives: imported
trackers only ever hold the current position, and their coordinates are
kept out of Home Assistant's database by default.

> This is a community project, not affiliated with GeoPulse.

## Requirements

- Home Assistant **2026.9** or newer
- A GeoPulse server reachable from Home Assistant
- A GeoPulse **API token** for the account you want to import from
  (GeoPulse → *Settings → API Tokens*)

## Installation

### HACS (recommended)

1. HACS → ⋮ → *Custom repositories* → add
   `https://github.com/blauvster/ha-geopulse`, type **Integration**.
2. Install **GeoPulse**, then restart Home Assistant.

### Manual

Copy `custom_components/geopulse/` into your Home Assistant `config/custom_components/`
folder and restart.

## Setup

*Settings → Devices & services → Add integration → GeoPulse.*

1. **Connection** — your GeoPulse URL (e.g. `http://geopulse.lan:8080`) and
   your API token. It's checked against the server straight away.
2. **Import** — what to bring into Home Assistant:
   - *Whole account*: one tracker with your latest position from any source.
   - *Per source type*: one tracker per GeoPulse location source type
     (OwnTracks, GPSLogger, …). GeoPulse can't tell apart two devices on the
     **same** source type, so e.g. two phones both on OwnTracks appear as one
     tracker.
   - *Friends*: one tracker per friend sharing their live location with you.
3. **Export** — which Home Assistant trackers to send to GeoPulse, then one
   screen per tracker for its device ID and token (see below).

Everything can be changed later under the integration's **Configure** menu,
which also has the poll interval (default 45 s) and the recorder setting.

### Exporting: one GeoPulse account per person

GeoPulse builds **one timeline per account**. A `device_id` is only a label
on each point — it doesn't separate devices on the map or in the timeline.
So trackers belonging to different people must go to **different GeoPulse
accounts**, or GeoPulse sees one person jumping between places.

For each person:

1. Create a GeoPulse account for them.
2. In *their* account: *Settings → Location Sources → Add New Source →
   Home Assistant*, and copy its token.
3. Paste that token on their tracker's screen during export setup.

One person's trackers (phone, watch, car) can share a token. To see
everyone together in GeoPulse and on the timeline card, share location and
timeline between the accounts in GeoPulse (*Friends*).

The token is the **location source** token, not an API token: it can only
add points to that account. Only friends you select are imported back into
Home Assistant, so a person you export doesn't loop back in unless you
choose them as an import.

## Timeline card

The card is included in the integration and available on every dashboard —
no extra resource to add. *Edit dashboard → Add card → GeoPulse timeline*,
or in YAML:

```yaml
type: custom:geopulse-card
title: Timeline        # optional
days: 1                # initial range ending today, 1–31
map_height: 320        # px
# entry_id: ...        # only needed with more than one GeoPulse entry
# tile_url: https://tiles.example.com/{z}/{x}/{y}.png
# tile_attribution: "© My tiles"
```

It shows you plus every friend who shares their **timeline** with you,
each in their GeoPulse colour: paths and stays on the map, and a
chronological list of stays, trips (with movement type, distance and
duration) and gaps in the data. Tap a person to hide them; tap a list
entry to find it on the map. Ranges go up to 31 days.

Maps use [OpenStreetMap](https://www.openstreetmap.org/) tiles by default,
loaded by your browser, under OSM's
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/);
for heavy use, point `tile_url` at another raster tile provider or your own.

**Only Home Assistant administrators can use the card**: it reads history
with your GeoPulse API token, which gives full access to your GeoPulse
account. The token stays in Home Assistant; the browser never sees it.

## Privacy and your data

- **History stays in GeoPulse.** Imported trackers poll the *current*
  position only.
- **Recorder:** by default, imported trackers' latitude, longitude,
  accuracy, altitude, speed and last-seen time are **not** stored in Home
  Assistant's database. Their zone state (`home`, `not_home`, zone name) and
  battery are, so presence history and the logbook keep working. To keep
  the zone state out as well:

  ```yaml
  recorder:
    exclude:
      entity_globs:
        - device_tracker.geopulse_*
  ```

  Turning off *Exclude imported trackers from the recorder* (Configure →
  Settings) records everything.
- **Tokens** are stored in Home Assistant's config entries like other
  integrations' credentials. The API token has full access to your GeoPulse
  account — GeoPulse has no read-only tokens.
- **Diagnostics** (*⋮ → Download diagnostics*) contain no tokens, server
  address or coordinates.

## Behaviour details

- **GeoPulse unreachable:** imported trackers keep their last position for
  5 minutes (or 3 poll intervals if longer), so a short outage doesn't
  trigger "left home" automations, then become unavailable.
- **Rejected API token:** Home Assistant asks you to re-authenticate.
- **Export retry queue** (on by default): points that can't be delivered are
  queued on disk and retried with back-off (30 s up to 15 min), in order,
  per GeoPulse account — one account's problem doesn't hold up the others.
  Up to 5000 points per account are kept. A rejected location source token
  shows up under *Settings → Repairs*.
- **Exported points** are only sent when a tracker actually moves (not for
  battery-only changes); trackers without coordinates (e.g. router-based
  presence) aren't exported.
- **Speed** is in m/s on imported trackers.

## Known limitations

- Two devices on the same GeoPulse source type show up as one imported
  tracker (GeoPulse's API doesn't expose which device a point came from).
- GeoPulse requires an altitude and a battery level on every exported
  point. When Home Assistant doesn't have them, the integration sends
  altitude `0` and battery `0` %.
- Home Assistant is removing `battery_level` from trackers in 2027.7; after
  that, exported trackers will mostly report battery `0` to GeoPulse.
- No history backfill: exporting starts from setup, it doesn't replay older
  Home Assistant history.
- GeoPulse "saved places" aren't synced with Home Assistant zones.

## Troubleshooting

- Turn on debug logs:
  ```yaml
  logger:
    logs:
      custom_components.geopulse: debug
  ```
- *Cannot connect*: check the URL from Home Assistant's point of view
  (container networking, reverse proxy path, http vs https).
- The card says "Only Home Assistant administrators…": log in as an
  administrator (see [Timeline card](#timeline-card)).
- Export not arriving: check *Settings → Repairs* and the logs for
  "Export to GeoPulse … failed".

Please include diagnostics when [opening an issue](https://github.com/blauvster/ha-geopulse/issues).

## Development

Tests need a POSIX host (Home Assistant's test plugin imports `fcntl`) and
run in Docker:

```sh
docker/run-tests.sh            # full suite; extra args go to pytest
docker/run-tests.sh -k export
```

- `docker/compose.dev.yml` runs a development Home Assistant (2026.9.4)
  with the integration mounted live; its config lives in `.ha-config/`
  (git-ignored). `docker/ha-token.sh` prints a fresh access token for it.
- `docker/probe.sh` checks a real GeoPulse server's API against what the
  integration expects — read-only, with coordinates, names and tokens
  redacted. It reads `GEOPULSE_URL` and `GEOPULSE_READ_TOKEN` from
  `.env.local` (git-ignored).
- The card is plain JavaScript with no build step
  (`custom_components/geopulse/frontend/geopulse-card.js`). Leaflet 1.9.4
  is vendored next to it under its BSD-2 licence.
- Design notes and verified GeoPulse API details: [Plan.md](Plan.md),
  [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).

Releases follow [Semantic Versioning](https://semver.org/); see
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). Leaflet is © Volodymyr Agafonkin and contributors,
[BSD-2-Clause](custom_components/geopulse/frontend/vendor/LEAFLET_LICENSE).
