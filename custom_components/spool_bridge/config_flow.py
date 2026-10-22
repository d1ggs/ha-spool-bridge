"""Config flow: Spoolman + printer + phone. Options flow: filament name rules and the Bambuddy link."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector, SelectSelectorConfig, SelectSelectorMode, TextSelector, TextSelectorConfig, TextSelectorType,
)

from .bambuddy import BambuddyClient, BambuddyError
from .const import (
    BAMBULAB_DOMAIN, CONF_BAMBUDDY_KEY, CONF_BAMBUDDY_URL, CONF_NAME_RULES, CONF_NOTIFY_SERVICE,
    CONF_PRINTER_SERIAL, CONF_SPOOLMAN_URL, DEFAULT_BAMBUDDY_URL, DEFAULT_SPOOLMAN_URL, DOMAIN,
)
from .naming import RuleError, parse_rules
from .spoolman import SpoolmanClient, SpoolmanError


def _select(options: list[str]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


class SpoolBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SpoolBridgeOptionsFlow:
        return SpoolBridgeOptionsFlow()

    def _printers(self) -> list[str]:
        devices = list(dr.async_get(self.hass).devices)
        return sorted(ident[1] for d in devices if d.via_device_id is None
                      for ident in d.identifiers if ident[0] == BAMBULAB_DOMAIN)

    def _notify_services(self) -> list[str]:
        return sorted(s for s in self.hass.services.async_services().get("notify", {}) if s.startswith("mobile_app_"))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = SpoolmanClient(async_get_clientsession(self.hass), user_input[CONF_SPOOLMAN_URL])
            try:
                await client.info()
                await client.ensure_extra_fields()
            except SpoolmanError:
                errors["base"] = "spoolman"
            else:
                await self.async_set_unique_id(user_input[CONF_PRINTER_SERIAL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Spool Bridge", data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_SPOOLMAN_URL, default=DEFAULT_SPOOLMAN_URL): str,
            vol.Required(CONF_PRINTER_SERIAL): _select(self._printers()),
            vol.Required(CONF_NOTIFY_SERVICE): _select(self._notify_services()),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class SpoolBridgeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            data = {CONF_NAME_RULES: user_input.get(CONF_NAME_RULES, "")}
            key = (user_input.get(CONF_BAMBUDDY_KEY) or "").strip()
            url = (user_input.get(CONF_BAMBUDDY_URL) or "").strip() or DEFAULT_BAMBUDDY_URL
            try:
                parse_rules(data[CONF_NAME_RULES])
            except RuleError as err:
                errors[CONF_NAME_RULES], detail = "invalid_rules", str(err)
            if not errors and key:
                client = BambuddyClient(async_get_clientsession(self.hass), url, key)
                try:
                    await client.printer_id(self.config_entry.data[CONF_PRINTER_SERIAL])
                except BambuddyError as err:
                    errors["base"], detail = "bambuddy", str(err)
                else:
                    data.update({CONF_BAMBUDDY_URL: url, CONF_BAMBUDDY_KEY: key})
            if not errors:
                return self.async_create_entry(data=data)
        current = user_input or self.config_entry.options
        schema = vol.Schema({
            vol.Optional(CONF_NAME_RULES, description={"suggested_value": current.get(CONF_NAME_RULES, "")}):
                TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(CONF_BAMBUDDY_URL,
                         description={"suggested_value": current.get(CONF_BAMBUDDY_URL, DEFAULT_BAMBUDDY_URL)}): str,
            vol.Optional(CONF_BAMBUDDY_KEY, description={"suggested_value": current.get(CONF_BAMBUDDY_KEY, "")}):
                TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors,
                                    description_placeholders={"error": detail})
