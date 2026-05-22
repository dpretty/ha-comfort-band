# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.8.0** adds a model-predictive controller as the third decision layer in the stack. Each refresh, MPC enumerates a small action space (`idle`, `heat → band's high edge`, `cool → band's low edge`), simulates each forward over `mpc_horizon_minutes` (default 20 min) using the v0.6 thermal slopes, and picks the action that maximises projected time-in-band. Behind a new opt-in `switch.{zone}_mpc_enabled` (default OFF) layered on top of `learning_enabled`; cold-start gated by `binary_sensor.{zone}_mpc_ready` (True once enough samples in idle / heat / cool segments have accumulated). v0.7's predictor remains the fallback during warm-up. v0.8 also captures the climate's `fan_mode` attribute in each sample (recorded but unused — v0.9 will partition slopes by `(action, fan_mode)` and grow MPC's action space to include fan-mode candidates). Builds on the v0.1 core (per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules), v0.2 schedule WS subscription, v0.3 profile CRUD, v0.4 apparent-temperature support, v0.5 cross-mode min-cycle dwell, v0.6 learned thermal-slope estimator, and v0.7 passive drift acceptance.

The Lovelace card lives in a separate repo — install [dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card) via HACS as a Dashboard Custom Repository.

Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over.
