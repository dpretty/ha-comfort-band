# Comfort Band

Per-room HVAC band-control for Home Assistant. Heats below a low threshold, cools above a high threshold, idles in between — giving heat pumps and mini-splits margin instead of fighting to hold a single setpoint.

**v0.1.0** is the first end-to-end release: integration + Lovelace card. The integration provides per-zone entities, asymmetric-deadband hysteresis, profile-driven schedules, and an `import_legacy` service for migrating from hand-rolled YAML. The card adds a compact tile + expanded modal with Now / Schedule / Profiles / Insights tabs (drag the dual-handle slider to override; tap the timeline to edit transitions; switch profiles with one tap).

This repo publishes **both** an Integration and a Dashboard plugin from one source. Add the URL twice in HACS as a Custom Repository — once under Integrations, once under Frontend.

Defaults to **shadow mode** per zone (`switch.{zone}_enabled = off`) so you can verify decisions side-by-side with your existing setup before cutting over.
