# Comfort Band

A Home Assistant custom integration for per-room HVAC band-control.

## What it does

Most thermostats hold a single setpoint, which makes heat pumps and mini-splits short-cycle as the room temperature drifts above and below the target. Comfort Band lets you define a **low/high band per room**: the system heats when the room falls below the low, cools when it climbs above the high, and stays idle (`fan_only`) in between.

Each zone gets its own band, override, and per-profile schedule. A **profile** (e.g. `home`, `away`, `sleep`) lets you swap every zone's schedule with a single tap or service call.

## Status

**v0.1.0 — first end-to-end release.** Integration + Lovelace card. The integration logic is the same as v0.0.2 — full coordinator, per-zone entities, profile schedules, override timers, legacy importer. New in v0.1.0: a custom Lovelace card with a compact tile and an expanded modal (Now / Schedule / Profiles / Insights tabs).

Every zone still defaults to **shadow mode** (`switch.{zone}_enabled = off`): the integration computes the heat/cool/idle decision and updates the `current_action` sensor, but does **not** call `climate.set_hvac_mode`. Flip the switch on per zone when you're ready to cut over.

## Installation

This repo publishes **two HACS items** from one source:

1. **Integration** — the Python side. Adds the `comfort_band` entities and services.
2. **Lovelace plugin** — the `comfort-band-card` dashboard card.

HACS doesn't auto-detect dual-content repos, so you add the URL **twice** as a Custom Repository:

1. In HACS: **Integrations → ⋮ → Custom repositories** → add `https://github.com/dpretty/ha-comfort-band`, category **Integration** → install → restart HA.
2. **HACS → Frontend → ⋮ → Custom repositories** → add the same URL, category **Dashboard** → install. The bundle lands at `/hacsfiles/ha-comfort-band/comfort-band-card.js`; HACS adds the resource automatically.
3. **Settings → Devices & Services → Add Integration → Comfort Band**.
4. Add `type: custom:comfort-band-card` cards to your dashboard, one per zone.

## The card

```yaml
type: custom:comfort-band-card
zone: gym         # required — the zone slug from step 3
compact: false    # optional — set true for a tile that doesn't expand on tap
```

Tap the tile → modal with four tabs:

- **Now** — big band gauge, current room temp + action chip, dual-handle slider for manual low/high (drag = start an override), Cancel-override button, 1h/3h/6h duration presets.
- **Schedule** — 24-hour timeline of the active profile's transitions. Tap empty → add. Tap a point → edit (precise time/low/high + delete). Long-press → delete. All edits persist via `comfort_band.set_schedule` (atomic full-schedule replacement).
- **Profiles** — list every profile, switch with one tap (fires `comfort_band.set_profile`). Profile create / rename / delete is deferred to v0.2 (needs new services).
- **Insights** — wraps HA's built-in `history-graph` for the last 24 h of `room_temperature`. v0.2 will replace this with a custom uPlot chart shaded by `current_action`.

Card config can also be edited via the dashboard's visual editor (zone dropdown + compact-mode toggle).

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
