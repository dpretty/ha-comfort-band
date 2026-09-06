# Comfort Band

A Home Assistant custom integration for per-room HVAC band-control.

## What it does

Most thermostats hold a single setpoint, which makes heat pumps and mini-splits short-cycle as the room temperature drifts above and below the target. Comfort Band lets you define a **low/high band per room**: the system heats when the room falls below the low, cools when it climbs above the high, and stays idle (`fan_only`) in between.

Each zone gets its own band, override, and per-profile schedule. A **profile** (e.g. `home`, `away`) lets you swap every zone's schedule with a single tap or service call.

## Status

**v0.16.0** makes losing a room sensor **visible**. A zone whose temperature sensor goes unavailable can't control at all — the decider has no reading, so nothing decides — and until now that was completely silent: no log line, no entity, nothing in the UI. Diagnosed from a master bedroom that drifted several degrees below its band for hours after its Matter/Thread sensor dropped off the mesh, with nothing anywhere to say the zone had stopped controlling.

A new **`binary_sensor.{zone}_room_sensor_unavailable`** (device class `problem`, first-class rather than diagnostic) turns that into something you can notify on, and the sensor's availability edge is now logged, naming the entity to check. Pair the sensor with a `for:` of a few minutes to ride out routine blips:

```yaml
- trigger: state
  entity_id: binary_sensor.mbr_room_sensor_unavailable
  to: "on"
  for: "00:05:00"
```

Three details worth knowing. The log is rate-limited to one edge per five minutes, because a dying battery or a weak mesh link crosses that boundary hundreds of times an hour — but it compares against what was last *logged* rather than against reality, so a blip that used up the budget can't hide the outage that follows it. A non-finite reading — `nan` or `±inf` — now counts as no reading. `nan` parses as a float but compares false against everything, so hysteresis simply latches whatever it was already doing: a heating zone heats indefinitely, an idle one idles while the room drifts, and `−inf` commands heat outright. In every case nothing alerted, because the sensor looked like it was reporting. And the entity deliberately stays available on stale data when a refresh fails, but only in the alarming direction — a stale `on` is worth keeping, while a stale `off` would assert "no problem" about a room that has since gone dark.

**Scope note.** This makes the failure *visible*; it does not change what the zone does about it. A zone that goes blind mid-cycle keeps running whatever was last commanded, with the head unit's own thermostat still regulating to that setpoint — the behaviour before this release. Releasing the cycle automatically turned out to need considerably more care than it appears to (seventeen review rounds' worth), so it is deferred rather than rushed.

**v0.15.0** fixes an MPC failure mode where a zone could **idle while sitting several degrees below its band**. The learned recovery slopes (how fast the room warms while heating / cools while cooling) are WLS-fit per action segment. A zone that only ever gets brief, sub-minute heat blips (e.g. a slow-to-heat gym chasing a steep morning band) accumulates just a handful of noisy heat samples whose fit comes out **negative** — i.e. the model believes *heating cools the room*. MPC's forward simulation then projects the `heat` candidate downward and `idle` upward and picks **idle**, stranding the room below band (and starving the estimator of the clean heat samples that would correct the slope — a self-reinforcing loop). v0.15.0 adds a **sign guard**: a heating segment can't have a non-positive slope and a cooling segment can't have a non-negative one, so such an estimate is discarded (`recovery_slope_heat`/`_cool` → `None`, `method_* = "rejected"` on `sensor.{zone}_thermal_slope`). With the slope gone, MPC falls back to the reactive predictor, which heats when below band — so the room starts closing the gap immediately instead of idling. Idle (passive-drift) slopes keep both signs. **Behaviour change for MPC zones:** where a wrong-sign recovery slope was previously trusted, the zone now heats/cools toward band instead of idling; zones with clean slope data are unaffected. (Diagnosed live from a gym that sat ~5 °C under its morning band for an hour while `recovery_slope_heat` read −0.1 to −0.9 °C/hr.)

**v0.14.1** is a fast-follow hardening of v0.14.0, two fixes. (1) **Profile rename/delete/clone now track shared schedules.** Renaming a profile (e.g. `home`→`casa`) already re-keyed each zone's own schedules; it now does the same for the per-profile slots *inside* shared schedules — so a zone assigned to a shared schedule keeps resolving the shared band instead of silently dropping to its manual band. Deleting a (non-default) profile likewise clears its slot from shared schedules, and cloning a profile seeds the cloned slot into them (an independent copy), exactly as it already did for each zone's own schedules. (2) **Transition-time validation tightened.** The `at`-field regex previously admitted impossible hours `24:00`–`29:59`, which passed the schema and then raised a raw `ValueError` deep in parsing; it now rejects them cleanly at the schema layer, with a defence-in-depth parser that surfaces a `ServiceValidationError` for any value that bypasses the schema (pre-existing on `main`, unrelated to shared schedules). No new entities; no behaviour change for valid existing setups.

**v0.14.0** adds **named shared schedules** so rooms that share air can target one band over the day instead of fighting. Until now schedules were stored strictly per `(zone, profile)`, so two open-door bedrooms with "the same" plan were independent copies that drifted — one heating while the other cooled. Now you create a first-class named schedule (`comfort_band.create_shared_schedule`, e.g. *Bedrooms*) and assign rooms to it with a new per-zone `select.{zone}_schedule_assignment` (options `Own schedule` + every shared name; default `Own schedule`). An assigned zone resolves its bands from the shared schedule live — edit it once (`comfort_band.set_schedule` with `shared_id` instead of `zone`, or the card's schedule editor) and **every** member room follows in the same refresh. Shared schedules stay profile-aware (home/away still resolve separately), feed MPC lookahead/pre-heat and band-ramp smoothing identically to own schedules, and a per-zone override still wins over the shared band. It's an **additive overlay, not a migration**: each zone keeps its own `schedules` untouched and reverts to them losslessly the moment you switch back to `Own schedule`, a dangling assignment falls back to own→manual rather than erroring, and there's no behaviour change until you assign a room (`STORAGE_VERSION` unchanged). Management services: `create_shared_schedule` (optionally seeded by deep-copying a zone's own schedules), `rename_shared_schedule` (a pure name update — assignments are keyed by a stable id, so renames never detach a room), `delete_shared_schedule` (refuses while rooms are assigned unless `cascade: true`, which unassigns them first), and `assign_schedule`. The assignment select also exposes `schedule_id` + a `shared_schedules` summary (id / name / member rooms) as attributes so the companion card can render membership without extra round-trips. The websocket `get_schedule` / `subscribe_schedule` commands accept a `shared_id` too, and a shared edit fans out to every card subscribed to that schedule. The card UI for this lands in a follow-up release; the integration contract is back-compatible, so the current card keeps working unchanged.

**v0.13.0** adds opt-in, per-zone **deterministic fan-boost**. Comfort Band has always commanded `hvac_mode` + setpoint but never `fan_mode`, so a mini-split sat at whatever fan the user/unit last set. Now, when the applied action is heat/cool it commands a configured **active** fan (faster recovery), and when it drops to idle (`fan_only`) it commands a quieter **idle** fan — fixing the reported gym annoyance where a manually-raised fan stayed at full speed during fan-only air-cycling. A `switch.{zone}_fan_control_enabled` (default OFF) gates it; `select.{zone}_active_fan_mode` / `_idle_fan_mode` source their options live from the climate's `fan_modes` and store the chosen **string** (robust to a unit re-ordering its modes). Skip-on-`None` is intentional and asymmetric — set just `idle_fan_mode` to quiet a unit while air-cycling and leave the active side untouched. The command is guarded: skipped when the mode isn't in the unit's current `fan_modes` (unavailable / fanless / stale value), or already equals the current `fan_mode` (no redundant calls to cloud/mesh-routed units), and a unit that rejects `set_fan_mode` mid-transition is logged, not fatal. It only ever changes the `fan_mode` attribute, which the manual-edit detector ignores (v0.10.1), so it can never flush the learning buffer. One shared "active" fan covers heat + cool; default OFF means no behaviour change for existing zones.

**v0.12.0** persists the **idle slope** so MPC stays ready through a heating chase. `binary_sensor.{zone}_mpc_ready` needs an `idle_slope` *and* a recovery slope present in the same 90-min sample window — but a heating-dominated room (the gym chasing a rising morning band) produces only 1–3 min idle blips (below `SLOPE_MIN_SAMPLES`), and any sustained overnight idle ages out of the window before the morning rise. So `idle_slope` went `None` exactly when pre-heat was needed and MPC silently fell back to the reactive predictor. The idle (passive heat-loss) rate is a *slow-changing* property, so the integration now remembers the last good one beyond the window: when the live estimate is `None`, a recent persisted value is substituted (via `dataclasses.replace`, tagged `method_idle="cached"`) so readiness survives the chase. Persisted to storage (survives restarts), expired after 24 h, and cleared on every buffer flush (manual edit / sensor swap) so a stale model can't mislead MPC. `sensor.{zone}_thermal_slope` gains `idle_slope_source` (`live` / `cached` / `none`) and `idle_slope_cached_age_min`. Only the idle slope is persisted — recovery slopes change faster and are always present during a chase. The cache is scoped to MPC: `mpc.is_ready` / `mpc.plan` and the thermal-slope diagnostics consume it, but the reactive hysteresis and the v0.7 predictor always run on the **live** slopes — so a zone not running MPC sees no control change, and a cached idle can never silently suppress a reactive heat/cool call.

**v0.11.0** adds **comfort feedback** — the data-collection foundation for the v3 auto-learning loop. A new `comfort_band.record_feedback(zone, label)` service logs how the current band feels (`too_hot` / `just_right` / `too_cold`), enriching each entry with the live room temperature, effective band, and action so a later aggregator can correlate patterns without re-deriving context. Entries persist in a **separate, capped** `comfort_band.feedback` Store (kept out of the main config so the append-only history can never bloat or corrupt it; trimmed to the most-recent 2000). The read side is a `comfort_band/get_feedback(zone, since?)` websocket command. The companion card surfaces three pills on the Now tab. No behaviour change for existing zones — pure capture for a future "you keep marking 22:00 too warm — lower the band?" nudge.

**v0.10.1** fixes a learning-buffer flush bug that kept MPC dormant on HVACs that vary their own fan speed. The manual-edit detector (`_on_climate_state_change`) compared `fan_mode`, so an HVAC modulating its fan autonomously — or reporting a different `fan_mode` in `fan_only` (idle) vs `heat`/`cool` — was misread as a manual edit and **flushed the entire sample buffer on every idle↔active transition**. The buffer therefore never held idle *and* recovery samples simultaneously, so `idle_slope` stayed `None`, `binary_sensor.{zone}_mpc_ready` never turned on, and MPC silently fell back to the reactive v0.7 predictor (no schedule-lookahead pre-heat). The detector now compares **only `hvac_mode` + `target_temp`** — still catching genuine manual setpoint/mode edits and physical-remote edits — while `fan_mode` continues to be captured per-sample (for future `(action, fan_mode)` slope partitioning). After upgrading, the buffer accumulates across fan changes; `mpc_ready` flips on once enough idle + recovery samples land, and anticipatory pre-heat engages.

**v0.10.0** adds **band-ramp smoothing** so schedule transitions can ease in instead of stepping instantly. Set `number.{zone}_band_ramp_minutes` to a non-zero value (e.g. 30) and the (low, high) band edges interpolate linearly within ±ramp/2 of each transition: a 4 °C overnight setback rising at 06:00 sharp becomes a 30-min shoulder from 05:45-06:15 instead of a wall. HVAC has time to ease into the new setpoint rather than chasing a sudden deficit. MPC's schedule lookahead (`upcoming_bands`) honours the same smoothing so its cost function sees the ramp shape too. Per-zone, opt-in: default 0 keeps the v0.9.x stepped behaviour for existing users. Range 0-120 minutes. Implemented in `schedule.resolve` / `schedule.upcoming_bands` with an optional `ramp_minutes` kwarg — predictor, hysteresis, and MPC consume the smoothed bands naturally via `effective_low` / `effective_high`. No new entities or behaviour changes for zones that don't opt in.

**v0.9.2** makes the room-temperature sensor swappable post-add. Settings → Devices & Services → Comfort Band → "<zone> (zone)" → Configure now shows both the room-temperature sensor (required) and the humidity sensor (optional, clearable). Picking a different temp sensor flushes the zone's sample buffer so the slope estimator restarts with clean data from the new sensor — MPC's `mpc_ready` goes False until samples accumulate again (typically a few hours of normal operation). Schedules, profiles, manual overrides, and learning state are preserved. Pairs with the v0.9.1 diagnostics: if `sensor.{zone}_thermal_slope` shows `std_dev_idle ≈ 0` across many samples, the room sensor is masking drift — this OptionsFlow lets you swap to a finer sensor (e.g. a dedicated 0.1 °C sensor instead of the climate entity's built-in 0.5 °C reading) in one click.

**v0.9.1** adds per-segment diagnostics to `sensor.{zone}_thermal_slope` so users can spot when a slope estimate is based on resolution-limited data. New attributes: `sample_count_idle / recovery_heat / recovery_cool` (samples behind each per-action slope, vs the existing aggregate), `std_dev_idle / recovery_heat / recovery_cool` (standard deviation of sample temperatures in each segment, °C), and `method_idle / recovery_heat / recovery_cool` (always `"wls"` or `"none"` in v0.9.1, reserved string for future fallback methods). The signature failure mode this surfaces: a 0.5 °C-resolution sensor on a slowly drifting room can read the same quantized value for hours, producing many samples with `std_dev ≈ 0` — the WLS slope reports 0 not because the room is stable but because the sensor can't see the drift. MPC will defer to the reactive predictor + hysteresis in that regime. A wider fix (extending sample retention beyond 90 min so the slope estimator sees crossings outside the WLS window) is deferred to v1.0 — investigation showed the narrower fallback approaches don't help the realistic "no crossings in 90 min" case. **No behaviour change for zones with well-resolved sensors** — diagnostics-only release.

**v0.9.0** teaches the v0.8 MPC to look ahead over the active profile's schedule. Each refresh, the coordinator computes the per-minute `(low, high)` band the schedule resolves to over `mpc_horizon_minutes`, and `mpc.plan` feeds that into the cost function so each candidate action is scored against the band that will be active at end-of-horizon — not the band frozen at the snapshot. A heat-only zone with the morning band rising from `(16, 19)` to `(20, 22)` at 07:00 now starts heating *before* the rise instead of catching up after, closing the user-reported "morning pre-heat didn't happen" issue. Same release adds an **idle-preference tie-break** in the cost function: when `idle` achieves full-horizon in-band coverage (within one simulation step), it wins over heat / cool even if a recovery action lands closer to band midpoint. Closes the symmetric "cool fired in winter when the room would have settled passively" report — MPC stops burning compressor cycles on margin gains the room already has. Default `mpc_horizon_minutes` bumped 20 → 60 to give the lookahead enough runway for pre-heat ramps (existing zones keep their explicit value via presence-keyed backfill — only new zones pick up the new default). No new entities, no new user-facing knobs. **Behaviour change for existing v0.8.1 users with `mpc_enabled = ON`:** idle-preference fires whenever both idle and a recovery action achieve full-horizon coverage, so steady-state operation will see noticeably fewer heat/cool cycles than v0.8.1 (which favoured the recovery action via midpoint tie-break). Existing zones also keep their 20-min horizon — the schedule lookahead is technically active but only sees 20 min ahead, so pre-heat ramps for schedule transitions fire later than they would with the new 60-min default. Bumping `number.{zone}_mpc_horizon_minutes` toward 60 unlocks the full pre-heat window.

**v0.8.1.** Relaxes the v0.8.0 MPC cold-start gate so heat-only and cool-only zones activate MPC after the first idle + matching-recovery segment accumulates (was: required all three slopes). Per-refresh safety bail-out when the room genuinely needs the missing direction. No behaviour change for fully-equipped zones.

**v0.8.0** adds a **model-predictive controller** as a third decision layer in the stack. Each refresh, MPC enumerates a small action space (`idle`, `heat → band's high edge`, `cool → band's low edge`), simulates each forward over `mpc_horizon_minutes` (default 20 min) using the v0.6 thermal slopes, and picks the action that maximises projected time-in-band. Where v0.7's predictor anticipates *individual decisions*, MPC scores *whole cycles* — so it can pick `idle` even when the predictor would fire heat, or fire `heat` early when the predictor would wait. Behind a new opt-in `switch.{zone}_mpc_enabled` (default OFF) layered on top of `learning_enabled`. New diagnostic surfaces: `sensor.{zone}_mpc_action` (always populated for shadow comparison), `binary_sensor.{zone}_mpc_ready` (True once enough data has accumulated — typically a few hours of normal operation), and `number.{zone}_mpc_horizon_minutes` (range 10-60). The v0.7 predictor and v0.5 min-cycle gates still apply on top of MPC. Samples now also record the climate's `fan_mode` attribute (recorded but unused in v0.8 — v0.9 will partition slopes by `(action, fan_mode)` so MPC's action space can grow to include fan-mode candidates and close [#17](https://github.com/dpretty/ha-comfort-band/issues/17)). `climate.set_temperature` calls are now rounded to the climate entity's `target_temp_step` attribute (fallback 0.5 °C) so what we ask for matches what the HVAC actually pursues.

**v0.7.1.** Docs and test-isolation polish on top of v0.7.0; no behaviour change.

**v0.7.0** adds **passive drift acceptance** to the v0.6 predictive controller. When the room has crossed the deadband but the predictor's slope says we'll naturally return to band within `lookahead_minutes`, the predictor now suppresses the heat / cool call hysteresis would otherwise issue — letting the room recover on its own. Bounded by a per-zone comfort floor (`number.{zone}_passive_tolerance`, default 0.5 °C, set to 0 to disable): we'll never tolerate drift further than that from the band. Reuses the existing `learning_enabled` gate; default behaviour is unchanged for users who haven't opted into predictive control. **Behaviour change for existing v0.6 users with `learning_enabled = ON`:** the 0.5 °C default tolerance silently enables passive acceptance on next load; set `number.{zone}_passive_tolerance` to `0` to restore v0.6's "always defer to hysteresis on band exits" behaviour.

**v0.6.0** added **predictive control via a learned thermal slope** ([#11](https://github.com/dpretty/ha-comfort-band/issues/11)). Per-zone rolling-window slope estimator (segmented by HVAC action) projects room temperature forward by `lookahead_minutes` (default 5) and triggers heat/cool earlier when an idle drift will cross the deadband, or releases earlier when a recovery rate will overshoot the band. Three new entities per zone: `sensor.{zone}_thermal_slope` (°C/h with per-action slopes in attributes), `sensor.{zone}_predicted_action` (always populated for shadow-comparison against `current_action`), `number.{zone}_lookahead_minutes` (range 2-15). Gated by the existing `switch.{zone}_learning_enabled` — default OFF, so behaviour is unchanged unless you flip the switch. v0.5 cross-mode gate still applies on top of predictor decisions. A manual edit to the climate entity (outside our path) flushes the sample buffer so the slope estimator stays honest.

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
| `number` | `override_hours`, `deadband_below`, `deadband_above`, `min_cycle_minutes`, `cross_mode_min_minutes`, `lookahead_minutes`, `passive_tolerance`, `mpc_horizon_minutes`, `band_ramp_minutes` | Tunables |
| `sensor` | `effective_low`, `effective_high` | Active band (override or schedule) |
| `sensor` | `room_temperature` | Diagnostic mirror of the source sensor. Carries `humidity_sensor` (the configured entity_id, or null) as a state attribute. |
| `sensor` | `apparent_temperature` | Steadman 1994 "feels like". Equals room temp when no humidity sensor is configured. |
| `sensor` | `override_ends` | Timestamp; null when no override |
| `sensor` | `current_action` | `heating` / `cooling` / `idle` / `unknown` |
| `sensor` | `thermal_slope` | Current learned slope (°C/h). Attributes: `idle_slope`, `recovery_slope_heat`, `recovery_slope_cool`, `sample_count`, `window_minutes`, `last_updated`. v0.9.1 added per-segment diagnostics: `sample_count_idle / recovery_heat / recovery_cool`, `std_dev_idle / recovery_heat / recovery_cool` (°C), `method_idle / recovery_heat / recovery_cool` (`"wls"` or `"none"`). None for the first ~5-10 min after install/restart. |
| `sensor` | `predicted_action` | What the predictor would issue right now. Always populated; flip `learning_enabled` ON to forward to climate. |
| `sensor` | `mpc_action` | What MPC would issue right now. Always populated (falls back to predictor's decision while `mpc_ready` is False); flip `mpc_enabled` ON (in addition to `learning_enabled`) to forward to climate. |
| `binary_sensor` | `override_active` | True while override is in effect |
| `binary_sensor` | `mpc_ready` | True when MPC has `idle_slope` and at least one recovery slope. Heat-only zones reach this after idle + heat segments accumulate; cool-only zones after idle + cool; fully-equipped zones after all three. Typically a few hours of normal operation. (v0.8.0 required all three slopes — the v0.8.1 relaxation lets unilateral-mode zones activate MPC.) |
| `binary_sensor` | `room_sensor_unavailable` | True while the configured room sensor isn't reporting, so this zone has stopped controlling. Device class `problem`, and deliberately not diagnostic — it's the signal worth notifying on. (v0.16.0) |
| `button` | `cancel_override` | Press to immediately end an override |
| `switch` | `enabled` | Master kill — defaults OFF (shadow mode) |
| `switch` | `learning_enabled` | Gates the v0.6 predictive controller. When ON, anticipated heat/cool decisions reach climate (subject to existing min-cycle and cross-mode gates). Anticipated idle releases bypass those gates so a cycle can always stop — same contract as v0.5. Default OFF. |
| `switch` | `mpc_enabled` | Gates the v0.8 model-predictive controller. Layered on `learning_enabled`: both must be ON, **and** `mpc_ready` must be True, for MPC's decision to reach climate. Default OFF. |
| `switch` | `use_apparent_temperature` | When ON, hysteresis decisions use the apparent value instead of the raw room reading. Falls back to room temp automatically if humidity is unavailable. Default OFF. |
| `switch` | `fan_control_enabled` | Gates the v0.13.0 deterministic fan-boost. When ON, the climate's fan mode is commanded by action (see the two selects below). Default OFF; a no-op until fan modes are picked. |
| `select` | `active_fan_mode` | Fan mode commanded while heating/cooling. Options come live from the climate's `fan_modes`, plus a leading `(none)` (= don't command this side). Unset by default. |
| `select` | `idle_fan_mode` | Fan mode commanded while idle (`fan_only`). Same options as `active_fan_mode`; pick a quiet level to stop full-speed air-cycling. Unset by default. |
| `select` | `schedule_assignment` | v0.14.0. Points the zone at a named shared schedule. Options: `Own schedule` (default) + every shared schedule's name. Exposes `schedule_id` and a `shared_schedules` summary (`id` / `name` / member rooms) as attributes for the card. |

Plus one global entity: `select.comfort_band_profiles_active_profile`.

## Predictive control (v0.6, extended in v0.7)

Each refresh, the integration appends a `(timestamp, decision_room, action)` sample to a per-zone rolling buffer (90 min, rate-limited to one sample per 60 s, persisted across restarts). It then computes three slopes via weighted least-squares regression with exponential recency weights (`τ = 20 min`):

- **`idle_slope`** — passive drift while idle (the room cooling overnight, warming in the sun).
- **`recovery_slope_heat`** — how fast the room rises while heat is running.
- **`recovery_slope_cool`** — how fast the room falls while cool is running.

When `switch.{zone}_learning_enabled` is ON, the predictor projects `decision_room + slope × lookahead_minutes` forward (where `decision_room` is the apparent temperature when `use_apparent_temperature` is ON, otherwise the raw room reading) and:

- **Anticipates startup**: while idle, if the projection crosses the deadband edge (`low - deadband_below` or `high + deadband_above`), it triggers heat / cool *now* instead of waiting.
- **Anticipates shutoff**: while heating or cooling, if the projection overshoots the band edge (`high` or `low`), it releases to idle *now* so the room peaks at the edge instead of past it.
- **Passive drift acceptance** (v0.7): when the room has already crossed the deadband but the slope says it'll return to band within the lookahead window, the predictor stays idle and lets the room recover on its own (rather than firing heat / cool that would interrupt the natural drift). Bounded by `number.{zone}_passive_tolerance` (default 0.5 °C; 0 disables) — we'll never tolerate drift further than that from the band edge, even if the slope says recovery is "in progress". A second jitter guard requires the forecast to move the room by at least 0.1 °C toward the band, so a sensor-noise slope can't spuriously suppress a real heat call.
  - Concrete example: with `low = 20 °C` and the defaults (`passive_tolerance = 0.5`, `deadband_below = 0.3`), heat is suppressed only when the room is at or above 19.5 °C and below 19.7 °C (`low - deadband_below`) AND the slope is positive enough that projection lands inside the band; below 19.5 °C hysteresis always fires.
  - Caveat: the slope reflects the most recent ~90 min of samples. A sharp change in heat load (cold snap, opened window) won't show in the slope until the buffer catches up; passive acceptance may briefly suppress a legitimate heat call until the comfort floor is hit. The 0.5 °C default bounds the worst-case dip.

`sensor.{zone}_predicted_action` is populated *regardless* of `learning_enabled`, so you can shadow-compare against `sensor.{zone}_current_action` before flipping the switch on. Tune `number.{zone}_lookahead_minutes` (default 5, range 2-15): higher values trigger heat/cool sooner — useful for slow-responding systems (underfloor heating, large rooms) where temperature reacts to commands several minutes after the fact. Lower values are appropriate for fast-responding mini-splits, or if you find the predictor over-eager.

Existing gates still apply on top: predictor-anticipated heat is still subject to `min_cycle_minutes`, and predictor-anticipated heat↔cool flips are still subject to `cross_mode_min_minutes`.

**Known limitation:** if you manually change `hvac_mode` or `target_temp` on the climate entity (outside Comfort Band), the integration logs an INFO message and flushes the sample buffer — the slope estimator needs ~90 min of contiguous data to re-converge. Setting it back via a Comfort Band number entity is fine; the buffer is only flushed on external state changes. (`fan_mode` is **not** in this list as of v0.10.1: many HVACs vary their own fan speed autonomously or report a different fan_mode in `fan_only` vs `heat`/`cool`, so comparing fan_mode flushed the buffer on every idle↔active transition and starved MPC of the idle+recovery sample mix it needs. fan_mode is still captured per-sample for future `(action, fan_mode)` slope partitioning.)

## Model-predictive control (v0.8)

Where the v0.7 predictor anticipates *individual decisions*, v0.8's MPC scores *whole cycles*. Each refresh, with both `learning_enabled` and `mpc_enabled` ON and `mpc_ready` True:

1. **Enumerate** a small action space — currently `{idle, heat → inputs.high, cool → inputs.low}`. (v0.9 will grow this to include per-fan-mode candidates.)
2. **Simulate** each candidate forward over `mpc_horizon_minutes` (default 60 min as of v0.9.0; existing zones keep their explicit value from earlier versions) by integrating at 1-minute steps using the matching learned slope (`idle`, `recovery_heat`, or `recovery_cool`). From v0.9.0, the band scored against at each step reflects the active profile's schedule at that minute — so a step crossing a band transition is judged against the band edges that will be active AT that step, not frozen at refresh time.
3. **Score** by minutes-in-band over the horizon, with a midpoint-distance tie-break to stabilise indeterminate cases.
4. **Pick** the highest-scoring action.

Why heat targets the band's **high** edge (not `low`): the target_temp value goes into `climate.set_temperature`. Aiming the climate at `low` lets its own internal hysteresis release heat as soon as the room reaches `low`, leaving the room oscillating across the band edge (the original v0.7 gym observation). Aiming higher means the climate keeps heating until *we* issue idle. The MPC's cost function re-evaluates idle every refresh and picks it once projected drift-down stays inside band longer than projected continued heating. Net effect: longer heat cycles, fewer compressor starts, and (usually) room oscillation that stays inside the band.

**Cold-start gate (v0.8.1+).** MPC needs `idle_slope` (the cost-function baseline) and at least one recovery slope (`recovery_heat` or `recovery_cool`) to compare candidates. Heat-only zones get MPC the moment idle + heat slopes accumulate; cool-only zones once idle + cool are available; fully-equipped zones pick from all three candidates. Until then `binary_sensor.{zone}_mpc_ready` is `off` and the coordinator silently falls back to the v0.7 predictor. The `mpc_action` sensor still populates during the warm-up (it mirrors the predictor's decision while not ready) so you can flip `mpc_enabled` on whenever you're ready — MPC takes over the moment the gate flips.

**Safety bail-out (v0.8.1+).** If the room is clearly outside band on a side whose recovery slope hasn't accumulated (e.g. a heat-only gym suddenly needs cooling), MPC defers to the predictor for that refresh — the predictor's hysteresis fires the correct direction reactively. `mpc_ready` stays `on` (MPC is still equipped to act in normal scenarios); the bail-out is observable by `mpc_action` mirroring `predicted_action` during the brief out-of-band episode.

**Recommended rollout.** Enable `learning_enabled` first; watch `predicted_action` track `current_action` for a few days. Then enable `mpc_enabled` and compare `mpc_action` against `predicted_action` (visible in the device's diagnostic section). The two should agree most of the time but periodically diverge — MPC stays idle longer, fires heat earlier, cools to a different edge. Once the divergence pattern looks right, leave `mpc_enabled` on and tune `mpc_horizon_minutes` (longer horizon = more conservative).

**Energy trade-off.** Targeting the band's opposite edge means heat / cool cycles run longer than in v0.7 — fewer cycles but more energy per cycle. v0.9's fan-mode extension will add a fan-speed penalty to the cost function so MPC can prefer "heat at fan_low for longer" over "heat at fan_high for shorter" when both score similar time-in-band.

Existing gates still apply on top: MPC-anticipated heat is still subject to `min_cycle_minutes`, and MPC-anticipated heat↔cool flips are still subject to `cross_mode_min_minutes`. Setpoint commands are rounded to the climate entity's `target_temp_step` (fallback 0.5 °C) before being issued so our `_last_command_state` snapshot matches what the climate will actually settle at.

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

Schedule mutations: `set_schedule`, `add_transition`, `update_transition`, `remove_transition` — each targets **either** a zone's own schedule (`zone:`) **or** a shared schedule (`shared_id:`); supply exactly one. Per-zone override control: `start_override` / `cancel_override`. Comfort feedback (v0.11.0): `record_feedback` (logs `too_hot` / `just_right` / `too_cold` for the v3 learning loop; read back via the `comfort_band/get_feedback` websocket command). Profile management: `set_profile` (switch active), `create_profile`, `clone_profile`, `rename_profile`, `delete_profile`. Shared schedules (v0.14.0): `create_shared_schedule` (optional `seed_from_zone` deep-copies that zone's own schedules), `rename_shared_schedule`, `delete_shared_schedule` (refuses while rooms are assigned unless `cascade: true`), `assign_schedule` (point a zone at a shared schedule, or back to its own with `shared_id` omitted). All zone-scoped services take the bare zone slug, not an entity_id. See `services.yaml` or **Developer Tools → Services** for the full schemas.

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
