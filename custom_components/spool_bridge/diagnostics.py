"""Diagnostics (no tokens, serials, RFIDs or ids)."""

from homeassistant.components.diagnostics import async_redact_data

REDACT = {"token", "email", "printer_serial"}


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    return {"entry": async_redact_data(dict(entry.data), REDACT), "bridge": entry.runtime_data.diagnostics()}
