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
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_KIND,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    DOMAIN,
    ENTRY_KIND_PROFILE_MANAGER,
    ENTRY_KIND_ZONE,
)

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZONE_NAME): selector.TextSelector(),
        vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["climate"])
        ),
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class=["temperature"])
        ),
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
