"""The X2D as seen through ha-bambulab: trays, print status, tray profile writes."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import AMS_TYPE_BY_MODEL, BAMBULAB_DOMAIN
from .model import SlotRef, tray_color

_AMS_TRAY_RE = re.compile(r"^[^_]+_(?P<serial>[^_]+)_AMS_(?P<ams>[^_]+)_tray_(?P<n>\d+)$")
_EXT_RE = re.compile(r"^[^_]+_(?P<serial>[^_]+)_ExternalSpool(?P<n>\d*)_external_spool$")
_AMS_INDEX_RE = re.compile(r"_AMS_(?P<n>\d+)$")
_HEX32 = re.compile(r"^[0-9A-F]{32}$")


@dataclass(frozen=True)
class TrayInfo:
    entity_id: str
    slot: SlotRef
    ams_serial: str  # "" for external holders
    ams_type: int  # cloud amsType; 0 for external holders


@dataclass(frozen=True)
class TrayState:
    empty: bool
    rfid: str | None  # Bambu tray UUID (32 hex) for official spools
    filament_id: str
    material: str
    color: str
    name: str
    tray_weight: int
    remain: int  # percent, -1 unknown


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_tray_state(state: Any) -> TrayState | None:
    """None when the sensor is missing or unavailable - never treat that as empty."""
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    attrs = state.attributes
    uuid = str(attrs.get("tray_uuid") or "").upper()
    rfid = uuid if _HEX32.match(uuid) and uuid.strip("0") else None
    return TrayState(
        empty=bool(attrs.get("empty")),
        rfid=rfid,
        filament_id=str(attrs.get("filament_id") or ""),
        material=str(attrs.get("type") or ""),
        color=str(attrs.get("color") or ""),
        name=str(attrs.get("name") or ""),
        tray_weight=_int(attrs.get("tray_weight"), 0),
        remain=_int(attrs.get("remain"), -1),
    )


class Printer:
    def __init__(self, hass: HomeAssistant, serial: str) -> None:
        self.hass = hass
        self.serial = serial

    def _entity(self, unique_id: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("sensor", BAMBULAB_DOMAIN, unique_id)

    @property
    def status_entity(self) -> str | None:
        return self._entity(f"{self.serial}_print_status")

    @property
    def progress_entity(self) -> str | None:
        return self._entity(f"{self.serial}_print_progress")

    def name(self) -> str:
        devices = dr.async_get(self.hass).async_get_devices(identifiers={(BAMBULAB_DOMAIN, self.serial)})
        device = devices[0] if devices else None
        return (device and (device.name_by_user or device.model)) or "Printer"

    def trays(self) -> list[TrayInfo]:
        ent_reg, dev_reg = er.async_get(self.hass), dr.async_get(self.hass)
        found: list[TrayInfo] = []
        for entry in ent_reg.entities.values():
            if entry.platform != BAMBULAB_DOMAIN or entry.domain != "sensor" or entry.disabled:
                continue
            uid = entry.unique_id or ""
            if (m := _AMS_TRAY_RE.match(uid)) and m.group("serial") == self.serial:
                device = dev_reg.async_get(entry.device_id) if entry.device_id else None
                index = _AMS_INDEX_RE.search(device.name or "") if device else None
                if index is None:
                    continue
                found.append(TrayInfo(
                    entry.entity_id,
                    SlotRef("ams", int(index.group("n")) - 1, int(m.group("n")) - 1),
                    m.group("ams"),
                    AMS_TYPE_BY_MODEL.get(device.model or "", 0),
                ))
            elif (m := _EXT_RE.match(uid)) and m.group("serial") == self.serial:
                found.append(TrayInfo(entry.entity_id, SlotRef("ext", int(m.group("n") or 1) - 1), "", 0))
        return sorted(found, key=lambda t: t.slot)

    def tray_for(self, slot: SlotRef) -> TrayInfo | None:
        return next((t for t in self.trays() if t.slot == slot), None)

    def tray_by_entity(self, entity_id: str) -> TrayInfo | None:
        return next((t for t in self.trays() if t.entity_id == entity_id), None)

    def tray_state(self, tray: TrayInfo) -> TrayState | None:
        return parse_tray_state(self.hass.states.get(tray.entity_id))

    def status(self) -> str | None:
        state = self.hass.states.get(self.status_entity) if self.status_entity else None
        return state.state if state else None

    def progress(self) -> int | None:
        state = self.hass.states.get(self.progress_entity) if self.progress_entity else None
        value = _int(state.state, -1) if state else -1
        return value if value >= 0 else None

    async def set_filament(
        self, tray: TrayInfo, *, filament_id: str, color_hex: str | None,
        material: str | None, nozzle_temp: int | None,
    ) -> None:
        low, high = (nozzle_temp - 10, nozzle_temp + 10) if nozzle_temp else (190, 240)
        await self.hass.services.async_call(
            BAMBULAB_DOMAIN, "set_filament",
            {
                "entity_id": tray.entity_id, "tray_info_idx": filament_id,
                "tray_color": tray_color(color_hex), "tray_type": material or "PLA",
                "nozzle_temp_min": low, "nozzle_temp_max": high,
            },
            blocking=True,
        )
