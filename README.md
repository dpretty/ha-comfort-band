# Comfort Band

A Home Assistant custom integration for per-room HVAC band-control.

## What it does

Most thermostats hold a single setpoint, which makes heat pumps and mini-splits short-cycle as the room temperature drifts above and below the target. Comfort Band lets you define a **low/high band per room**: the system heats when the room falls below the low, cools when it climbs above the high, and stays idle (`fan_only`) in between.

Each zone gets its own band, override, and per-profile schedule. A **profile** (e.g. `home`, `away`) lets you swap every zone's schedule with a single tap or service call.

## Status

**v0.6.0.** Adds **predictive control via a learned thermal slope** ([#11](https://github.com/dpretty/ha-comfort-band/issues/11)). Per-zone rolling-window slope estimator (segmented by HVAC action) projects room temperature forward by `lookahead_minutes` (default 5) and triggers heat/cool earlier when an idle drift will cross the deadband, or releases earlier when a recovery rate will overshoot the band. Three new entities per zone: `sensor.{zone}_thermal_slope` (°C/h with per-action slopes in attributes), `sensor.{zone}_predicted_action` (always populated for shadow-comparison against `current_action`), `number.{zone}_lookahead_minutes` (range 2-15). Gated by the existing `switch.{zone}_learning_enabled` — default OFF, so behaviour is unchanged unless you flip the switch. v0.5 cross-mode gate still applies on top of predictor decisions. A manual edit to the climate entity (outside our path) flushes the sample buffer so the slope estimator stays honest.

**v0.5.0** added **cross-mode min-cycle dwell** to suppress rapid `heat ↔ cool` mode flips ([#16](https://github.com/dpretty/ha-comfort-band/issues/16)). New per-zone `number.{zone}_cross_mode_min_minutes` defaults to the zone's current `min_cycle_minutes` (8 by default) — `heat → cool` and `cool → heat` transitions now wait that many minutes after the last action. Idle releases (`heat → idle`, `cool → idle`) still fire immediately so a heat or cool cycle can always stop. **Behaviour change for existing users:** mode flips previously fired instantly; set the new entity to `0` to restore that.

**v0.4.0** added apparent-temperature support: a per-zone optional **humidity sensor** (configurable via ConfigFlow or a new OptionsFlow on existing zones), a new `sensor.{zone}_apparent_temperature` (Steadman 1994 simplified; equals room temp when no humidity is configured), and a per-zone `switch.{zone}_use_apparent_temperature` toggle that feeds the apparent value into hysteresis decisions instead of the raw room reading. Decisions fall back to the raw reading automatically when the humidity sensor is unavailable. Also adds `switch.{zone}_learning_enabled` — a master gate for the v0.4+ learning cluster (#9, #11); no decision effect at the v0.4.0 release (v0.6.0 activated it for predictive control).

**v0.3.0** added full profile CRUD: four new services (`create_profile`, `clone_profile`, `rename_profile`, `delete_profile`) and a new `SIGNAL_PROFILE_LIST_CHANGED` dispatcher signal so the singleton select entity (and any subscribed cards) re-render on every profile mutation. The `default_profile` is now tracked per-store and survives renames — whatever profile holds that role cannot be deleted. Built-in profiles narrowed from `home / away / sleep` to **`home` + `away`**; existing installs keep any `sleep` profile they had as a normal user profile that can now be renamed or deleted.

**v0.2.0** added a `comfort_band/subscribe_schedule` websocket command so the card receives live schedule updates from any source without polling.

Every zone still defaults to **shadow mode** (`switch.{zone}_enabled = off`): the integration computes the heat/cool/idle decision and updates the `current_action` sensor, but does **not** call `climate.set_hvac_mode`. Flip the switch on per zone when you're ready to cut over.

## Companion card

A Lovelace card lives in a separate repo: **[dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card)**. Compact tile + expanded modal with Now / Schedule / Profiles / Insights tabs. Install it via HACS as a Dashboard Custom Repository — see that repo's README for steps.

## Installation

1. In HACS: **Integrations → ⋮ → Custom repositories**.
2. URL: `https://github.com/dpretty/ha-comfort-band`. Category: **Integration**.
3. Install. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Comfort Band**.

## Setup walkthrough

1. **Add the profile manager** (one-time): Add Integration → Comfort Band → *Set up profile manager*. This creates `select.comfort_band_profiles_active_profile` with the built-in profiles `home` + `away`. Use the card's Profiles tab (or the `create_profile` / `clone_profile` services) to add your own.
2. **Add a zone**: Add Integration → Comfort Band → *Add a zone*. You'll be asked for:
   - **Zone name (slug)**: lowercase letters / digits / underscores, e.g. `office`. Becomes part of every entity_id.
   - **Climate entity**: the `climate.*` entity Comfort Band will eventually drive (e.g. a Mitsubishi mini-split).
   - **Room-temperature sensor**: a `sensor.*` of `device_class: temperature` to watch.
   - **Humidity sensor (optional)**: a `sensor.*` of `device_class: humidity`. When set, enables apparent temperature ("feels like"). Can be added / changed / cleared later via **Configure** on the zone entry.
3. Repeat for every room.

## Entities created per zone

| Platform | Entity | Notes |
|---|---|---|
| `number` | `manual_low`, `manual_high` | UI writes start an override |
| `number` | `override_hours`, `deadband_below`, `deadband_above`, `min_cycle_minutes`, `cross_mode_min_minutes`, `lookahead_minutes` | Tunables |
| `sensor` | `effective_low`, `effective_high` | Active band (override or schedule) |
| `sensor` | `room_temperature` | Diagnostic mirror of the source sensor. Carries `humidity_sensor` (the configured entity_id, or null) as a state attribute. |
| `sensor` | `apparent_temperature` | Steadman 1994 "feels like". Equals room temp when no humidity sensor is configured. |
| `sensor` | `override_ends` | Timestamp; null when no override |
| `sensor` | `current_action` | `heating` / `cooling` / `idle` / `unknown` |
| `sensor` | `thermal_slope` | Current learned slope (°C/h). Attributes: `idle_slope`, `recovery_slope_heat`, `recovery_slope_cool`, `sample_count`, `window_minutes`, `last_updated`. None for the first ~5-10 min after install/restart. |
| `sensor` | `predicted_action` | What the predictor would issue right now. Always populated; flip `learning_enabled` ON to forward to climate. |
| `binary_sensor` | `override_active` | True while override is in effect |
| `button` | `cancel_override` | Press to immediately end an override |
| `switch` | `enabled` | Master kill — defaults OFF (shadow mode) |
| `switch` | `learning_enabled` | Gates the v0.6 predictive controller. When ON, anticipated heat/cool decisions reach climate (subject to existing min-cycle and cross-mode gates). Anticipated idle releases bypass those gates so a cycle can always stop — same contract as v0.5. Default OFF. |
| `switch` | `use_apparent_temperature` | When ON, hysteresis decisions use the apparent value instead of the raw room reading. Falls back to room temp automatically if humidity is unavailable. Default OFF. |

Plus one global entity: `select.comfort_band_profiles_active_profile`.

## Predictive control (v0.6)

Each refresh, the integration appends a `(timestamp, decision_room, action)` sample to a per-zone rolling buffer (90 min, rate-limited to one sample per 60 s, persisted across restarts). It then computes three slopes via weighted least-squares regression with exponential recency weights (`τ = 20 min`):

- **`idle_slope`** — passive drift while idle (the room cooling overnight, warming in the sun).
- **`recovery_slope_heat`** — how fast the room rises while heat is running.
- **`recovery_slope_cool`** — how fast the room falls while cool is running.

When `switch.{zone}_learning_enabled` is ON, the predictor projects `decision_room + slope × lookahead_minutes` forward (where `decision_room` is the apparent temperature when `use_apparent_temperature` is ON, otherwise the raw room reading) and:

- **Anticipates startup**: while idle, if the projection crosses the deadband edge (`low - deadband_below` or `high + deadband_above`), it triggers heat / cool *now* instead of waiting.
- **Anticipates shutoff**: while heating or cooling, if the projection overshoots the band edge (`high` or `low`), it releases to idle *now* so the room peaks at the edge instead of past it.

`sensor.{zone}_predicted_action` is populated *regardless* of `learning_enabled`, so you can shadow-compare against `sensor.{zone}_current_action` before flipping the switch on. Tune `number.{zone}_lookahead_minutes` (default 5, range 2-15): higher values trigger heat/cool sooner — useful for slow-responding systems (underfloor heating, large rooms) where temperature reacts to commands several minutes after the fact. Lower values are appropriate for fast-responding mini-splits, or if you find the predictor over-eager.

Existing gates still apply on top: predictor-anticipated heat is still subject to `min_cycle_minutes`, and predictor-anticipated heat↔cool flips are still subject to `cross_mode_min_minutes`.

**Known limitation:** if you manually change `hvac_mode` or `target_temp` on the climate entity (outside Comfort Band), the integration logs an INFO message and flushes the sample buffer — the slope estimator needs ~90 min of contiguous data to re-converge. Setting it back via a Comfort Band number entity is fine; the buffer is only flushed on external state changes.

### Profile manager entity attributes

`select.comfort_band_profiles_active_profile` carries two extra attributes the card (or any consumer) can read alongside the standard `options` / `state`:

- `default_profile` — the rename-aware fallback target. Initially `"home"`; tracks renames. The profile pointed to by this attribute cannot be deleted.
- `descriptions` — a `{profile_name: description}` map; each profile's optional description as set via `create_profile` / `clone_profile`.

The card uses presence of `default_profile` as a feature flag — if it's missing, the card knows it's talking to a pre-v0.3.0 install and hides the CRUD affordances.

## Importing a legacy schedule

If you're migrating from a hand-rolled YAML system that stored each zone's schedule as 48 `input_number` helpers (one each for low and high, per hour 00..23), the `comfort_band.import_legacy` service will collapse them into a transition list and seed the zone's **default profile** (initially `home`; tracks renames).

In **Developer Tools → Services**:

```yaml
service: comfort_band.import_legacy
data:
  zone: office          # Comfort Band zone slug
  source_zone_name: office  # slug used by your legacy input_number entities
```

Adjacent identical hours collapse into a single transition, so a flat 24-hour band yields one transition at `00:00`. The service raises if any helper is missing, unavailable, or non-numeric.

## Other services

Per-zone schedule mutations: `set_schedule`, `add_transition`, `update_transition`, `remove_transition`. Per-zone override control: `start_override` / `cancel_override`. Profile management: `set_profile` (switch active), `create_profile`, `clone_profile`, `rename_profile`, `delete_profile`. All zone-scoped services take the bare zone slug, not an entity_id. See `services.yaml` or **Developer Tools → Services** for the full schemas.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
mypy --strict custom_components/comfort_band
pytest tests/
```

A `pre-commit` hook runs `ruff` and a banned-words check on every commit. Add private terms (e.g. household member names) to `.banned-words.local` (gitignored).

## License

MIT — see [LICENSE](LICENSE).
