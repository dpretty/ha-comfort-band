"""Config flow.

Menu offers two paths:
  - "Set up profile manager" (singleton; only shown when none exists yet)
  - "Add zone" (one ConfigEntry per zone; slug uniqueness enforced)

Slugs are constrained to [a-z][a-z0-9_]{1,31} so they make safe entity-id
fragments downstream.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_KIND,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    DOMAIN,
    ENTRY_KIND_PROFILE_MANAGER,
    ENTRY_KIND_ZONE,
)

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# Shared selectors: same filters as the initial zone-add schema below, so
# the OptionsFlow form offers exactly the same entity choices for edits.
_TEMP_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["sensor"], device_class=["temperature"])
)
_HUMIDITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["sensor"], device_class=["humidity"])
)

_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZONE_NAME): selector.TextSelector(),
        vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["climate"])
        ),
        vol.Required(CONF_TEMP_SENSOR): _TEMP_SELECTOR,
        vol.Optional(CONF_HUMIDITY_SENSOR): _HUMIDITY_SELECTOR,
    }
)


class ComfortBandConfigFlow(ConfigFlow, domain=DOMAIN):
    """User-facing config flow for Comfort Band."""

    VERSION = 1

    async def async_step_user(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["zone"]
        if not self._has_profile_manager():
            menu.insert(0, "profile_manager")
        return self.async_show_menu(step_id="user", menu_options=menu)

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            slug = user_input[CONF_ZONE_NAME].strip().lower()
            if not _SLUG_RE.match(slug):
                errors[CONF_ZONE_NAME] = "invalid_slug"
            else:
                await self.async_set_unique_id(f"zone:{slug}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Comfort Band: {slug}",
                    data={
                        CONF_KIND: ENTRY_KIND_ZONE,
                        CONF_ZONE_NAME: slug,
                        CONF_CLIMATE_ENTITY: user_input[CONF_CLIMATE_ENTITY],
                        CONF_TEMP_SENSOR: user_input[CONF_TEMP_SENSOR],
                        # Optional — None if the user didn't pick one. Stored
                        # in entry data; the OptionsFlow below mirrors it
                        # under entry options for later edits.
                        CONF_HUMIDITY_SENSOR: user_input.get(CONF_HUMIDITY_SENSOR),
                    },
                )
        return self.async_show_form(step_id="zone", data_schema=_ZONE_SCHEMA, errors=errors)

    async def async_step_profile_manager(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id("profile_manager")
        self._abort_if_unique_id_configured()
        if user_input is None:
            return self.async_show_form(step_id="profile_manager")
        return self.async_create_entry(
            title="Comfort Band Profiles",
            data={CONF_KIND: ENTRY_KIND_PROFILE_MANAGER},
        )

    def _has_profile_manager(self) -> bool:
        return any(
            entry.data.get(CONF_KIND) == ENTRY_KIND_PROFILE_MANAGER
            for entry in self._async_current_entries()
        )

    @classmethod
    def async_supports_options_flow(cls, entry: ConfigEntry) -> bool:
        # Only zone entries have editable options today. The profile-manager
        # singleton has nothing user-tunable beyond what the card surfaces.
        return entry.data.get(CONF_KIND) == ENTRY_KIND_ZONE

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZoneOptionsFlow()


class ZoneOptionsFlow(OptionsFlow):
    """Edit a zone's sensor wiring after creation: room temperature
    (required, swapping flushes the sample buffer) and humidity (optional,
    clearable).

    Resolution order at read time is `entry.options[KEY]` falling back to
    `entry.data[KEY]` — so existing zones that set sensors at first-setup
    keep working, and an OptionsFlow edit wins thereafter. The same
    pattern is used by `__init__.py` when wiring the coordinator.

    Submitting the form persists BOTH sensors into options (so a user
    clearing a previously-set humidity sensor produces `{humidity: None}`
    rather than silently reverting to the data field). When the temp
    sensor changes vs the current effective value, the zone's sample
    buffer is cleared before persisting — mixing samples from two
    sensors at different resolutions / placements would bias the slope
    estimator. The change also pairs with v0.9.1's diagnostics: if
    `sensor.{zone}_thermal_slope`'s `std_dev_idle` sits near 0 across
    many samples, the sensor is masking drift and bumping it via this
    flow is the recommended remedy.

    Uses the modern HA convention: `self.config_entry` is resolved by the
    base class from `self.hass`, no `__init__` override required.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        current_temp = entry.options.get(CONF_TEMP_SENSOR, entry.data[CONF_TEMP_SENSOR])
        current_humidity = entry.options.get(
            CONF_HUMIDITY_SENSOR, entry.data.get(CONF_HUMIDITY_SENSOR)
        )
        if user_input is not None:
            new_temp = user_input[CONF_TEMP_SENSOR]
            # String equality on the EntitySelector's output is sufficient
            # — the widget returns the entity_id verbatim from the entity
            # registry (always lowercase, no whitespace), so there's no
            # casing / formatting ambiguity for "did the user change the
            # sensor?".
            if new_temp != current_temp:
                # Sensor swap: clear samples so the new sensor's data
                # isn't mixed with old-sensor samples at a different
                # resolution / offset. Correctness depends on the
                # reload-on-options-change listener firing before any
                # other refresh; the order is: persist samples=[] to
                # store → return async_create_entry → HA fires the
                # update listener → entry reload tears down the old
                # coordinator (clearing its _samples_cache) and
                # instantiates a fresh one that `load_samples` from
                # the now-empty store. MPC's `mpc_ready` then goes
                # False until the slope estimator accumulates fresh
                # samples (typically a few hours of normal operation,
                # per v0.8.0 README).
                #
                # `hass.data[DOMAIN].store` raises KeyError if the
                # integration's shared data wasn't initialised — HA
                # blocks the OptionsFlow when the entry failed setup,
                # so this is unreachable in practice. No defensive
                # guard for consistency with the rest of the codebase.
                store = self.hass.data[DOMAIN].store
                await store.async_update_zone(entry.data[CONF_ZONE_NAME], samples=[])
            return self.async_create_entry(
                title="",
                # Voluptuous omits the humidity key when its EntitySelector
                # is left empty. Normalise to None so the resolution above
                # sees a value (not a missing key that falls through to
                # entry.data — which would silently re-apply the
                # previously-saved sensor).
                data={
                    CONF_TEMP_SENSOR: new_temp,
                    CONF_HUMIDITY_SENSOR: user_input.get(CONF_HUMIDITY_SENSOR),
                },
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_TEMP_SENSOR, default=current_temp): _TEMP_SELECTOR,
                vol.Optional(
                    CONF_HUMIDITY_SENSOR,
                    description={"suggested_value": current_humidity},
                ): _HUMIDITY_SELECTOR,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
