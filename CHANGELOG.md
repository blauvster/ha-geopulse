# Changelog

Versions follow [Semantic Versioning](https://semver.org/). The version in
`custom_components/geopulse/manifest.json` is the source of truth (the card
declares the same one; a test keeps them in sync). Releases are GitHub
releases tagged `vX.Y.Z`, which is what HACS installs.

## 0.1.1 — 2026-09-29

- **Fix:** the timeline card could fail to load with "Custom element
  doesn't exist: geopulse-card" — intermittently, and more often with a
  warm browser cache. The card could register itself before Home
  Assistant's frontend had set up its own custom-element registry, which
  then never saw it. The card now waits for Home Assistant to boot before
  registering.

## 0.1.0 — 2026-09-29

First release.

- **Setup:** config flow with live validation, reauth and options; one
  entry per GeoPulse account.
- **Import:** GeoPulse positions as `device_tracker` entities — whole
  account, per source type, and friends sharing their live location.
  Current position only; coordinates kept out of Home Assistant's recorder
  by default. Last position kept through short GeoPulse outages.
- **Export:** Home Assistant `device_tracker`s to GeoPulse, one GeoPulse
  account (location-source token) per tracker, through a persistent retry
  queue per account.
- **Timeline card**, shipped with the integration:
  - map and stay/trip list for a date range, for you and friends sharing
    their timeline;
  - visual editor: people to show / hidden by default, default range,
    per-person colours, sections and their order, layout;
  - one card for small and full-screen dashboards: side by side when wide,
    fills panel views and sized sections-view cells;
  - "now" markers when the range includes today, grouped when people are
    in the same place, with auto-refresh;
  - access limited to chosen Home Assistant users (default: everyone
    logged in); the GeoPulse token never reaches the browser.
- Diagnostics without tokens, server address or coordinates.
