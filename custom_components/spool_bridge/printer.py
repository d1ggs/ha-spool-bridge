"""The X2D as seen through ha-bambulab: trays, print status, tray profile writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
from pathlib import Path
import re
import time
from typing import Any
from xml.etree import ElementTree
import zipfile

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import AMS_TYPE_BY_MODEL, BAMBULAB_DOMAIN, SET_FILAMENT_CONFIRM_S
from .model import SlotRef, tray_color

_AMS_TRAY_RE = re.compile(r"^[^_]+_(?P<serial>[^_]+)_AMS_(?P<ams>[^_]+)_tray_(?P<n>\d+)$")
_EXT_RE = re.compile(r"^[^_]+_(?P<serial>[^_]+)_ExternalSpool(?P<n>\d*)_external_spool$")
_AMS_INDEX_RE = re.compile(r"_AMS_(?P<n>\d+)$")
_HEX32 = re.compile(r"^[0-9A-F]{32}$")


_PLATE_GCODE_RE = re.compile(r"plate_(\d+)\.gcode$")
_MODEL_META_RE = re.compile(r'<metadata name="(Title|Designer)">(.*?)</metadata>', re.DOTALL)
_MODEL_HEAD_BYTES = 65536  # the metadata precedes the meshes in 3D/3dmodel.model


@dataclass(frozen=True)
class ModelInfo:
    title: str | None  # the model's name as published (MakerWorld title), not the file name
    designer: str | None
    plate: int | None
    plate_name: str | None
    file: str


def _plate_metadata(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(archive.read(name))
    except (KeyError, ElementTree.ParseError):
        return []
    return [{m.get("key"): m.get("value") for m in plate.findall("metadata")} for plate in root.iter("plate")]


def read_3mf_model(path: Path, plate: int | None) -> ModelInfo | None:
    """Title, designer and plate name from a sliced 3MF. ``plate`` is the plate the
    printer reports; without it, the plate the file was sliced for. Blocking I/O."""
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("3D/3dmodel.model") as model:
                head = model.read(_MODEL_HEAD_BYTES).decode("utf-8", "replace")
            if plate is None:
                index = next((p.get("index") for p in _plate_metadata(archive, "Metadata/slice_info.config")), None)
                plate = _int(index, 0) or None
            names = {p.get("plater_id"): p.get("plater_name") for p in
                     _plate_metadata(archive, "Metadata/model_settings.config")}
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    meta = {k: html.unescape(v).strip() or None for k, v in _MODEL_META_RE.findall(head)}
    return ModelInfo(title=meta.get("Title"), designer=meta.get("Designer"), plate=plate,
                     plate_name=names.get(str(plate)) or None, file=path.name)


_WEIGHT_AMS_RE = re.compile(r"^AMS (?P<unit>\d+) Tray (?P<slot>\d+)$")
_WEIGHT_EXT = {"External Spool": 254, "External Spool 2": 255}


def _weight_key_to_flat(name: str) -> int | None:
    """ha-bambulab print_weight attribute name -> flat slot index (AMS HT is out of scope)."""
    if name in _WEIGHT_EXT:
        return _WEIGHT_EXT[name]
    m = _WEIGHT_AMS_RE.match(name)
    if m:
        return (int(m.group("unit")) - 1) * 4 + (int(m.group("slot")) - 1)
    return None


class FilamentNotApplied(Exception):
    """The tray did not take the profile, although the service call returned."""


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
    active: bool = False  # the tray currently feeding the nozzle


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
        active=bool(attrs.get("active")),
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

    def print_weights(self) -> dict[int, float]:
        """Per-slot grams of the current/just-finished print, keyed by flat slot index
        (AMS unit*4 + slot; 254/255 external) to match the cloud job's amsDetailMapping.

        Read from ha-bambulab's print_weight sensor, which parses the sliced 3MF, so
        no Bambu cloud job history is needed."""
        entity = er.async_get(self.hass).async_get_entity_id(
            "sensor", BAMBULAB_DOMAIN, f"{self.serial}_print_weight")
        state = self.hass.states.get(entity) if entity else None
        if state is None:
            return {}
        out: dict[int, float] = {}
        for name, grams in state.attributes.items():
            flat = _weight_key_to_flat(name)
            try:
                value = float(grams)
            except (TypeError, ValueError):
                continue
            if flat is not None and value:
                out[flat] = value
        return out

    @property
    def model_source_entities(self) -> list[str]:
        """ha-bambulab sensors whose change means a different print or plate."""
        found = (self._entity(f"{self.serial}_gcode_file_downloaded"), self._entity(f"{self.serial}_gcode_file"))
        return [e for e in found if e]

    def _model_cache_roots(self) -> list[Path]:
        # ha-bambulab's file cache (coordinator.get_file_cache_directory), and the
        # local media dir it falls back to when /config/www is not writable.
        roots = [Path(self.hass.config.path("www/media/ha-bambulab", self.serial))]
        if media := self.hass.config.media_dirs.get("local"):
            roots.append(Path(media) / "ha-bambulab" / self.serial)
        return roots

    async def current_model(self) -> ModelInfo | None:
        """The model being (or last) printed, read from the 3MF ha-bambulab cached.

        ha-bambulab reports the G-code it extracted as "<size>-<name>.gcode.gcode"
        next to "<size>-<name>.gcode.3mf"; the printer's own G-code path names the
        plate ("/data/Metadata/plate_16.gcode")."""
        downloaded = self.hass.states.get(self._entity(f"{self.serial}_gcode_file_downloaded") or "")
        gcode = self.hass.states.get(self._entity(f"{self.serial}_gcode_file") or "")
        if downloaded is None or not downloaded.state.endswith(".gcode"):
            return None
        name = downloaded.state.removesuffix(".gcode") + ".3mf"
        plate = _PLATE_GCODE_RE.search(gcode.state) if gcode else None
        roots = self._model_cache_roots()

        def find_and_read() -> ModelInfo | None:
            for root in roots:
                for path in (root / "prints").rglob(name) if (root / "prints").is_dir() else ():
                    return read_3mf_model(path, int(plate.group(1)) if plate else None)
            return None

        return await self.hass.async_add_executor_job(find_and_read)

    def _binary_on(self, unique_id: str) -> bool:
        entity = er.async_get(self.hass).async_get_entity_id("binary_sensor", BAMBULAB_DOMAIN, unique_id)
        state = self.hass.states.get(entity) if entity else None
        return state is not None and state.state == "on"

    def control_blocked(self) -> bool:
        """True when the firmware blocks control commands (cloud mode, MQTT signing
        required). set_filament and other writes are then rejected or dropped.

        mqtt_encryption only says the firmware supports signing and stays on in
        LAN + Developer mode, where unsigned commands work; developer_lan_mode is
        ha-bambulab's reading of the printer's "signature required" flag."""
        return (self._binary_on(f"{self.serial}_mqtt_encryption")
                and not self._binary_on(f"{self.serial}_developer_lan_mode"))

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
        # ha-bambulab returns without an error when the printer blocks control
        # commands, so only the tray reporting the new profile proves it applied.
        deadline = time.monotonic() + SET_FILAMENT_CONFIRM_S
        while (state := self.tray_state(tray)) is None or state.filament_id != filament_id:
            if time.monotonic() >= deadline:
                reported = state.filament_id if state and state.filament_id else "nothing"
                raise FilamentNotApplied(f"the printer still reports {reported}")
            await asyncio.sleep(0.5)
