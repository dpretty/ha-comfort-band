# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.0.2** ships the full logic core: per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules, and an `import_legacy` service for migrating from hand-rolled YAML. Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over. No Lovelace card yet.
