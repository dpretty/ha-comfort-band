# Comfort Band

A Home Assistant custom integration for per-room HVAC band-control.

## What it does

Most thermostats hold a single setpoint, which makes heat pumps and mini-splits short-cycle as the room temperature drifts above and below the target. Comfort Band lets you define a **low/high band per room**: the system heats when the room falls below the low, cools when it climbs above the high, and stays idle (`fan_only`) in between.

Each zone gets its own band, override, and per-profile schedule. A **profile** (e.g. `home`, `away`, `sleep`) lets you swap every zone's schedule with a single tap or service call.

## Status

**v0.2.0.** Adds a `comfort_band/subscribe_schedule` websocket command so the card can receive live schedule updates from any source (a second card on another dashboard, an automation, Developer Tools → Services) without polling. The store fires a new `SIGNAL_ZONE_SCHEDULE_CHANGED` dispatcher signal on every persisted schedule write. The older `comfort_band/get_schedule` request/response command remains for back-compat. The integration logic itself is unchanged from v0.1 — full coordinator, per-zone entities, profile schedules, override timers, legacy importer.

Every zone still defaults to **shadow mode** (`switch.{zone}_enabled = off`): the integration computes the heat/cool/idle decision and updates the `current_action` sensor, but does **not** call `climate.set_hvac_mode`. Flip the switch on per zone when you're ready to cut over.

## Companion card

A Lovelace card lives in a separate repo: **[dpretty/ha-comfort-band-card](https://github.com/dpretty/ha-comfort-band-card)**. Compact tile + expanded modal with Now / Schedule / Profiles / Insights tabs. Install it via HACS as a Dashboard Custom Repository — see that repo's README for steps.

## Installation

1. In HACS: **Integrations → ⋮ → Custom repositories**.
2. URL: `https://github.com/dpretty/ha-comfort-band`. Category: **Integration**.
3. Install. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Comfort Band**.

## Setup walkthrough

1. **Add the profile manager** (one-time): Add Integration → Comfort Band → *Set up profile manager*. This creates `select.comfort_band_profiles_active_profile` with the built-in profiles `home` / `away` / `sleep`.
2. **Add a zone**: Add Integration → Comfort Band → *Add a zone*. You'll be asked for:
   - **Zone name (slug)**: lowercase letters / digits / underscores, e.g. `office`. Becomes part of every entity_id.
   - **Climate entity**: the `climate.*` entity Comfort Band will eventually drive (e.g. a Mitsubishi mini-split).
   - **Room-temperature sensor**: a `sensor.*` of `device_class: temperature` to watch.
3. Repeat for every room.

## Entities created per zone

| Platform | Entity | Notes |
|---|---|---|
| `number` | `manual_low`, `manual_high` | UI writes start an override |
| `number` | `override_hours`, `deadband_below`, `deadband_above`, `min_cycle_minutes` | Tunables |
| `sensor` | `effective_low`, `effective_high` | Active band (override or schedule) |
| `sensor` | `room_temperature` | Diagnostic mirror of the source sensor |
| `sensor` | `override_ends` | Timestamp; null when no override |
| `sensor` | `current_action` | `heating` / `cooling` / `idle` / `unknown` |
| `binary_sensor` | `override_active` | True while override is in effect |
| `button` | `cancel_override` | Press to immediately end an override |
| `switch` | `enabled` | Master kill — defaults OFF (shadow mode) |

Plus one global entity: `select.comfort_band_profiles_active_profile`.

## Importing a legacy schedule

If you're migrating from a hand-rolled YAML system that stored each zone's schedule as 48 `input_number` helpers (one each for low and high, per hour 00..23), the `comfort_band.import_legacy` service will collapse them into a transition list and seed the zone's `home` profile.

In **Developer Tools → Services**:

```yaml
service: comfort_band.import_legacy
data:
  zone: office          # Comfort Band zone slug
  source_zone_name: office  # slug used by your legacy input_number entities
```

Adjacent identical hours collapse into a single transition, so a flat 24-hour band yields one transition at `00:00`. The service raises if any helper is missing, unavailable, or non-numeric.

## Other services

`set_schedule`, `add_transition`, `update_transition`, `remove_transition` (per-zone, per-profile schedule mutations); `start_override` / `cancel_override` (per-zone); `set_profile` (global). All take the bare zone slug, not an entity_id. See `services.yaml` or **Developer Tools → Services** for the full schemas.

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
