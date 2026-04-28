# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.1.1** ships the integration core: per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules, an `import_legacy` service for migrating from hand-rolled YAML, and a `comfort_band/get_schedule` websocket command for the companion card.

The Lovelace card lives in a separate repo — install [dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card) via HACS as a Dashboard Custom Repository.

Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over.
