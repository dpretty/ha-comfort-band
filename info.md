# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.4.0** adds apparent temperature ("feels like") with an optional per-zone humidity sensor and a `use_apparent_temperature` switch that swaps which value drives heating / cooling decisions. Builds on the v0.1 core (per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules), v0.2 live-schedule WS subscription, and v0.3 full profile CRUD.

The Lovelace card lives in a separate repo — install [dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card) via HACS as a Dashboard Custom Repository.

Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over.
