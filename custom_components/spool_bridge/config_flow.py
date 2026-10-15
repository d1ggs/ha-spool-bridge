"""Config flow: Spoolman + printer + phone, Bambu login (or token import), migration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .bambu_cloud import (
    AuthFailed, BambuCloudClient, BambuCloudError, CloudflareBlocked, CodeIncorrect,
    EmailCodeRequired, TfaRequired,
)
from .const import (
    BAMBU_FILAMENTS_DOMAIN, BAMBULAB_DOMAIN, CONF_EMAIL, CONF_NOTIFY_SERVICE, CONF_PRINTER_SERIAL,
    CONF_REGION, CONF_SPOOLMAN_URL, CONF_TOKEN, DEFAULT_SPOOLMAN_URL, DOMAIN,
)
from .migration import describe, import_spools, plan_migration
from .printer import Printer
from .spoolman import SpoolmanClient, SpoolmanError


def _select(options: list[str]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


class SpoolBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._client: BambuCloudClient | None = None
        self._tfa_key = ""
        self._cloud: list[dict] = []

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
                self._data.update(user_input)
                return await self.async_step_bambu()
        schema = vol.Schema({
            vol.Required(CONF_SPOOLMAN_URL, default=DEFAULT_SPOOLMAN_URL): str,
            vol.Required(CONF_PRINTER_SERIAL): _select(self._printers()),
            vol.Required(CONF_NOTIFY_SERVICE): _select(self._notify_services()),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_bambu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["login"]
        if self.hass.config_entries.async_entries(BAMBU_FILAMENTS_DOMAIN):
            options.insert(0, "import_token")
        return self.async_show_menu(step_id="bambu", menu_options=options)

    async def async_step_import_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        source = self.hass.config_entries.async_entries(BAMBU_FILAMENTS_DOMAIN)[0].data
        self._data.update({CONF_REGION: source["region"], CONF_EMAIL: source["email"], CONF_TOKEN: source["token"]})
        return await self.async_step_migrate()

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_REGION] = user_input[CONF_REGION]
            self._data[CONF_EMAIL] = user_input[CONF_EMAIL].strip()
            self._client = BambuCloudClient(user_input[CONF_REGION])
            try:
                await self.hass.async_add_executor_job(self._client.login, self._data[CONF_EMAIL], user_input["password"])
            except EmailCodeRequired:
                return await self.async_step_code()
            except TfaRequired as err:
                self._tfa_key = err.tfa_key
                return await self.async_step_tfa()
            except CloudflareBlocked:
                errors["base"] = "cloudflare"
            except AuthFailed:
                errors["base"] = "invalid_auth"
            except BambuCloudError:
                errors["base"] = "cannot_connect"
            else:
                return await self._logged_in()
        schema = vol.Schema({
            vol.Required(CONF_REGION, default=self._data.get(CONF_REGION, "global")): _select(["global", "china"]),
            vol.Required(CONF_EMAIL, default=self._data.get(CONF_EMAIL, "")): str,
            vol.Required("password"): str,
        })
        return self.async_show_form(step_id="login", data_schema=schema, errors=errors)

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._client.login_with_code, self._data[CONF_EMAIL], user_input["code"].strip())
            except CodeIncorrect:
                errors["base"] = "code_incorrect"
            except BambuCloudError:
                errors["base"] = "cannot_connect"
            else:
                return await self._logged_in()
        return self.async_show_form(step_id="code", data_schema=vol.Schema({vol.Required("code"): str}),
                                    errors=errors, description_placeholders={"email": self._data[CONF_EMAIL]})

    async def async_step_tfa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._client.login_with_tfa, self._tfa_key, user_input["code"].strip())
            except CodeIncorrect:
                errors["base"] = "code_incorrect"
            except BambuCloudError:
                errors["base"] = "cannot_connect"
            else:
                return await self._logged_in()
        return self.async_show_form(step_id="tfa", data_schema=vol.Schema({vol.Required("code"): str}), errors=errors)

    async def _logged_in(self) -> ConfigFlowResult:
        self._data[CONF_TOKEN] = self._client.token
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data={**entry.data, CONF_TOKEN: self._client.token})
        return await self.async_step_migrate()

    async def async_step_migrate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        client = BambuCloudClient(self._data[CONF_REGION], self._data[CONF_TOKEN])
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                self._cloud = await self.hass.async_add_executor_job(client.get_spools)
            except BambuCloudError:
                return self.async_abort(reason="cannot_connect")
        else:
            if user_input["run_migration"]:
                imports, deletes = plan_migration(self._cloud)
                spoolman = SpoolmanClient(async_get_clientsession(self.hass), self._data[CONF_SPOOLMAN_URL])
                try:
                    await import_spools(spoolman, imports, self._data[CONF_PRINTER_SERIAL],
                                        Printer(self.hass, self._data[CONF_PRINTER_SERIAL]).name())
                    if deletes:
                        await self.hass.async_add_executor_job(client.delete_spools, [c["id"] for c in deletes])
                except (SpoolmanError, BambuCloudError):
                    errors["base"] = "migration_failed"
            if not errors:
                return self.async_create_entry(title="Spool Bridge", data=self._data)
        imports, deletes = plan_migration(self._cloud)
        return self.async_show_form(
            step_id="migrate", data_schema=vol.Schema({vol.Required("run_migration", default=False): bool}),
            errors=errors, description_placeholders={"plan": describe(imports, deletes)})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._data = dict(entry_data)
        return await self.async_step_login()
