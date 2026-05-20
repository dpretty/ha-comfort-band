# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.7.1** (docs and test-isolation polish; no behaviour change). **v0.7.0** extended the v0.6 predictive controller with passive drift acceptance: when the room has crossed the deadband threshold (`low - deadband_below` or `high + deadband_above`) but the slope says natural recovery will return us to band within the lookahead window, the predictor stays idle instead of firing heat / cool. Bounded by `number.{zone}_passive_tolerance` (default 0.5 °C; 0 disables). Gated by the existing `switch.{zone}_learning_enabled` — default behaviour is unchanged unless you've opted into predictive control. Builds on the v0.1 core (per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules), v0.2 schedule WS subscription, v0.3 profile CRUD, v0.4 apparent-temperature support, v0.5 cross-mode min-cycle dwell, and v0.6 learned thermal-slope estimator.

The Lovelace card lives in a separate repo — install [dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card) via HACS as a Dashboard Custom Repository.

Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over.
