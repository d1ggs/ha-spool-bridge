"""Bridge health sensors, and the model the printer is printing."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, SIGNAL_STATE

SENSORS = (("pending_jobs", "Pending usage deductions", None),
           ("last_sync", "Last cloud sync", SensorDeviceClass.TIMESTAMP),
           ("last_error", "Last error", None))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    async_add_entities([*(BridgeSensor(entry, key, name, dc) for key, name, dc in SENSORS),
                        CurrentModelSensor(entry)])


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


class CurrentModelSensor(SensorEntity):
    """Title of the model being printed, with designer and plate name, from the
    sliced 3MF. The printer itself only reports the file name."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Current model"
    _attr_icon = "mdi:cube-outline"

    def __init__(self, entry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-current_model"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Spool Bridge"}
        self._attributes: dict[str, Any] = {}
        self._refreshes = 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attributes

    async def async_refresh(self) -> None:
        # The download and plate sensors change moments apart and each read runs in
        # the executor; only the newest read may write, or a stale plate wins.
        self._refreshes += 1
        mine = self._refreshes
        info = await self._entry.runtime_data.printer.current_model()
        if mine != self._refreshes:
            return
        self._attr_native_value = info.title[:255] if info and info.title else None
        self._attributes = {"designer": info.designer, "plate": info.plate, "plate_name": info.plate_name,
                            "file": info.file} if info else {}
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        async def changed(_event) -> None:
            await self.async_refresh()

        self.async_on_remove(async_track_state_change_event(
            self.hass, self._entry.runtime_data.printer.model_source_entities, changed))
        await self.async_refresh()
