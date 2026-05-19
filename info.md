# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.6.0** adds predictive control via a learned thermal-slope estimator: the integration projects room temperature forward and triggers heat / cool earlier when an idle drift will cross the band, or releases earlier when a recovery rate would overshoot. Gated by `switch.{zone}_learning_enabled` (default OFF — opt in when you're ready). Builds on the v0.1 core (per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules), v0.2 schedule WS subscription, v0.3 profile CRUD, v0.4 apparent-temperature support, and v0.5 cross-mode min-cycle dwell.

The Lovelace card lives in a separate repo — install [dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card) via HACS as a Dashboard Custom Repository.

Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over.
