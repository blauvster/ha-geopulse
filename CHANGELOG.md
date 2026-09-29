# Changelog

Versions follow [Semantic Versioning](https://semver.org/). The version in
`custom_components/geopulse/manifest.json` is the source of truth (the card
declares the same one; a test keeps them in sync). Releases are GitHub
releases tagged `vX.Y.Z`, which is what HACS installs.

## 0.1.0 — unreleased

First release.

- Config flow with live validation, reauth and options; entries keyed by
  GeoPulse account.
- **Import:** GeoPulse positions as `device_tracker` entities — whole
  account, per source type, and friends sharing their live location.
  Current position only; coordinates kept out of HA's recorder by default.
- **Export:** HA `device_tracker`s to GeoPulse, one GeoPulse account (token)
  per tracker, through a persistent, per-account retry queue.
- **Timeline card:** map and stay/trip list for a date range, for you and
  friends sharing their timeline; shipped with the integration.
- Diagnostics without tokens, server URL or coordinates.
