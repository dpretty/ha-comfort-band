# Comfort Band

A Home Assistant custom integration for per-room HVAC band-control.

## What it does

Most thermostats hold a single setpoint, which makes heat pumps and mini-splits short-cycle as the room temperature drifts above and below the target. Comfort Band lets you define a **low/high band per room**: the system heats when the room falls below the low, cools when it climbs above the high, and stays idle in between.

Each zone gets its own band, schedule, and override. A "profile" (e.g. *home*, *away*, *sleep*) lets you swap every zone's schedule with a single tap.

## Status

**Early scaffolding.** The repo currently contains the integration skeleton, a placeholder config flow, and CI plumbing. Zone configuration, scheduling, hysteresis, profiles, and the Lovelace card are tracked as roadmap items in [Issues](https://github.com/dpretty/ha-comfort-band/issues).

Installing right now will create an empty entry that does nothing — wait for the v0.1 tag.

## Installation (preview)

1. In HACS: **Integrations → ⋮ → Custom repositories**.
2. URL: `https://github.com/dpretty/ha-comfort-band`. Category: **Integration**.
3. Install. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Comfort Band**.

## Configuration example (forthcoming)

Zones are configured one at a time via the UI. Sample zone names: `office`, `bedroom`, `lounge`, `studio`, `main`. No zone names are baked in — you choose your own.

## Development

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy --strict custom_components/comfort_band
pytest tests/
```

A `pre-commit` hook runs `ruff` and a banned-words check on every commit.

## License

MIT — see [LICENSE](LICENSE).
