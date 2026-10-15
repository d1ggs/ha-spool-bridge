"""Bridge health sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_STATE

SENSORS = (("pending_jobs", "Pending usage deductions", None),
           ("last_sync", "Last cloud sync", SensorDeviceClass.TIMESTAMP),
           ("last_error", "Last error", None))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    async_add_entities(BridgeSensor(entry, key, name, dc) for key, name, dc in SENSORS)


class BridgeSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key, name, device_class) -> None:
        self._entry, self._key = entry, key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_device_class = device_class
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Spool Bridge"}

    @property
    def native_value(self):
        value = self._entry.runtime_data.state.get(self._key)
        return value[:255] if isinstance(value, str) else value

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE, self.async_write_ha_state))
