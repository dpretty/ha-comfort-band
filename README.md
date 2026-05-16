# Comfort Band

A Home Assistant custom integration for per-room HVAC band-control.

## What it does

Most thermostats hold a single setpoint, which makes heat pumps and mini-splits short-cycle as the room temperature drifts above and below the target. Comfort Band lets you define a **low/high band per room**: the system heats when the room falls below the low, cools when it climbs above the high, and stays idle (`fan_only`) in between.

Each zone gets its own band, override, and per-profile schedule. A **profile** (e.g. `home`, `away`) lets you swap every zone's schedule with a single tap or service call.

## Status

**v0.3.0.** Adds full profile CRUD: four new services (`create_profile`, `clone_profile`, `rename_profile`, `delete_profile`) and a new `SIGNAL_PROFILE_LIST_CHANGED` dispatcher signal so the singleton select entity (and any subscribed cards) re-render on every profile mutation. The `default_profile` is now tracked per-store and survives renames — whatever profile holds that role cannot be deleted. Built-in profiles narrowed from `home / away / sleep` to **`home` + `away`**; existing installs keep any `sleep` profile they had as a normal user profile that can now be renamed or deleted.

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
