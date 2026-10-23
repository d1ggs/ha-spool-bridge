"""Spool Bridge: Spoolman <-> Bambu printer trays (via ha-bambulab) <-> Bambuddy."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bambuddy import BambuddyClient
from .bridge import Bridge
from .const import (
    CONF_BAMBUDDY_KEY, CONF_BAMBUDDY_URL, CONF_GATEWAY_PORT, CONF_PRINTER_SERIAL, CONF_SPOOLMAN_URL, DEFAULT_BAMBUDDY_URL, DOMAIN,
    SPOOLMAN_PROXY_HOST, SPOOLMAN_PROXY_PORT,
)
from .model import SlotRef
from .printer import Printer
from .spoolman import BAMBUDDY_FIELDS, SpoolmanClient, SpoolmanError
from .spoolman_proxy import SpoolmanProxy

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _slot(value: str) -> SlotRef:
    slot = SlotRef.parse(value)
    if slot is None:
        raise vol.Invalid("slot must look like ams:1:0 or ext:0")
    return slot


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    def bridge() -> Bridge:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            return entry.runtime_data
        raise HomeAssistantError("Spool Bridge is not set up")

    async def reconcile(call: ServiceCall) -> None:
        await bridge().request_reconcile(now=True)

    async def assign(call: ServiceCall) -> None:
        await bridge().assign(call.data["spool_id"], call.data["slot"])

    async def unassign(call: ServiceCall) -> None:
        await bridge().unassign(call.data["slot"])

    hass.services.async_register(DOMAIN, "reconcile", reconcile)
    hass.services.async_register(DOMAIN, "assign", assign, vol.Schema(
        {vol.Required("spool_id"): cv.positive_int, vol.Required("slot"): _slot}))
    hass.services.async_register(DOMAIN, "unassign", unassign, vol.Schema({vol.Required("slot"): _slot}))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v2 dropped the Bambu cloud: forget the account and token v1 stored."""
    if entry.version == 1:
        data = {k: v for k, v in entry.data.items() if k not in ("region", "email", "token")}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    spoolman = SpoolmanClient(session, entry.data[CONF_SPOOLMAN_URL])
    bambuddy_key = entry.options.get(CONF_BAMBUDDY_KEY)
    try:
        await spoolman.ensure_extra_fields(BAMBUDDY_FIELDS if bambuddy_key else ())
    except SpoolmanError as err:
        raise ConfigEntryNotReady(str(err)) from err
    bambuddy = proxy = None
    if bambuddy_key:
        bambuddy = BambuddyClient(session, entry.options.get(CONF_BAMBUDDY_URL) or DEFAULT_BAMBUDDY_URL, bambuddy_key)
        port = entry.options.get(CONF_GATEWAY_PORT) or SPOOLMAN_PROXY_PORT
        proxy = SpoolmanProxy(session, entry.data[CONF_SPOOLMAN_URL], SPOOLMAN_PROXY_HOST, port,
                              lambda spool_id, grams: entry.runtime_data.on_bambuddy_usage(spool_id, grams))
    bridge = Bridge(hass, entry, spoolman, Printer(hass, entry.data[CONF_PRINTER_SERIAL]),
                    bambuddy=bambuddy, proxy=proxy)
    entry.runtime_data = bridge
    await bridge.async_start()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options apply right away: name rules at the startup sync, Bambuddy on (re)start."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
