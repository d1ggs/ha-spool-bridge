"""Pure data helpers shared by all modules (no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

MATERIAL_DEFAULT_IDS = {
    "PLA": "GFL99", "PETG": "GFG99", "ABS": "GFB99",
    "ASA": "GFB98", "TPU": "GFU99", "PLA-CF": "GFL98",
}
FALLBACK_FILAMENT_ID = "GFL99"


@dataclass(frozen=True, order=True)
class SlotRef:
    """One physical spool position: an AMS slot or an external holder."""

    kind: str  # "ams" | "ext"
    unit: int  # AMS index (0-based) or external holder index (0-based)
    slot: int = 0  # slot within the AMS (0-based); 0 for external holders

    @property
    def key(self) -> str:
        if self.kind == "ams":
            return f"ams:{self.unit}:{self.slot}"
        return f"ext:{self.unit}"

    @classmethod
    def parse(cls, value: str | None) -> SlotRef | None:
        if not value:
            return None
        parts = value.split(":")
        try:
            if parts[0] == "ams" and len(parts) == 3:
                return cls("ams", int(parts[1]), int(parts[2]))
            if parts[0] == "ext" and len(parts) == 2:
                return cls("ext", int(parts[1]))
        except ValueError:
            return None
        return None

    def label(self, printer_name: str) -> str:
        if self.kind == "ams":
            return f"{printer_name} AMS {self.unit + 1} · slot {self.slot + 1}"
        return f"{printer_name} External {self.unit + 1}"

    @property
    def flat_index(self) -> int:
        """Bambu's flat tray index as used in cloud print jobs."""
        if self.kind == "ams":
            return self.unit * 4 + self.slot
        return 254 + self.unit


def slot_from_flat(ams: Any) -> SlotRef | None:
    """Cloud job `amsDetailMapping[].ams` -> slot (AMS HT units are out of scope)."""
    if isinstance(ams, bool) or not isinstance(ams, int):
        return None
    if 0 <= ams < 16:
        return SlotRef("ams", ams // 4, ams % 4)
    if ams in (254, 255):
        return SlotRef("ext", ams - 254)
    return None


BAMBUDDY_EXTERNAL_AMS = 255


def bambuddy_slot(slot: SlotRef) -> tuple[int, int]:
    """Bambuddy's (ams_id, tray_id): AMS units as-is, both external holders as AMS 255."""
    if slot.kind == "ams":
        return slot.unit, slot.slot
    return BAMBUDDY_EXTERNAL_AMS, slot.unit


def slot_from_bambuddy(ams_id: int, tray_id: int) -> SlotRef | None:
    """Inverse of bambuddy_slot (AMS HT units, 128+, are out of scope)."""
    if ams_id == BAMBUDDY_EXTERNAL_AMS:
        return SlotRef("ext", tray_id) if tray_id in (0, 1) else None
    if 0 <= ams_id < 8 and 0 <= tray_id < 4:
        return SlotRef("ams", ams_id, tray_id)
    return None


_TAG_RE = re.compile(r"^(?:spool-(\d+)|spool/show/(\d+))$")


def tag_to_spool_id(tag_id: str | None) -> int | None:
    """Spoolman spool id from an HA tag id (`spool-42` or `spool/show/42`)."""
    match = _TAG_RE.match((tag_id or "").strip())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?$")


def hex6(value: Any) -> str:
    """Bare upper-case RRGGBB, or "" for anything that is not a hex colour."""
    if not isinstance(value, str):
        return ""
    match = _HEX_RE.match(value.strip())
    return match.group(1).upper() if match else ""


def tray_color(color_hex: str | None) -> str:
    """Printer tray colour: RRGGBBAA."""
    return (hex6(color_hex) or "FFFFFF") + "FF"


def extra_get(entity: dict, key: str) -> str | None:
    """Decoded Spoolman extra field value ("" and null count as unset)."""
    raw = (entity.get("extra") or {}).get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if value is None or value == "":
        return None
    return str(value)


def extra_value(value: str | None) -> str | None:
    """Encode for a Spoolman PATCH; None removes the key."""
    return None if value is None else json.dumps(str(value))


def default_filament_id(material: str | None) -> str:
    return MATERIAL_DEFAULT_IDS.get((material or "").strip().upper(), FALLBACK_FILAMENT_ID)


_PROFILE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{3,7}$")


def preset_filament_id(value: str | None) -> str | None:
    """A printer filament id from a Bambuddy per-spool preset, or None.

    Bambuddy stores filament ids ("GFA01"), setting ids ("GFSA01_07") and
    presets the printer cannot take as a tray profile: cloud user presets
    (PFUS…/PFCN…), local preset numbers and Orca UUIDs."""
    base = (value or "").strip().split("_")[0].upper()
    if base.startswith(("PFUS", "PFCN")) or not _PROFILE_ID_RE.match(base):
        return None
    if base.startswith("GFS") and len(base) >= 5:
        return "GF" + base[3:]
    return base


def spool_filament_id(spool: dict) -> str:
    """The tray profile for a spool: a per-spool preset chosen in Bambuddy wins
    over the filament's profile id, then the material's generic profile."""
    filament = spool.get("filament") or {}
    return (preset_filament_id(extra_get(spool, "bambu_slicer_filament"))
            or extra_get(filament, "bambu_filament_id") or default_filament_id(filament.get("material")))


def cloud_payload(spool: dict) -> dict:
    """Bambu cloud library fields for a Spoolman spool (no id)."""
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or "Generic"
    material = filament.get("material") or "PLA"
    total = spool.get("initial_weight") or filament.get("weight") or 1000
    remaining = spool.get("remaining_weight")
    if remaining is None:
        remaining = total
    color = hex6(filament.get("color_hex")) or "FFFFFF"
    return {
        "filamentVendor": vendor,
        "filamentType": material,
        "filamentName": filament.get("name") or material,
        "filamentId": spool_filament_id(spool),
        "color": f"#{color}",
        "netWeight": max(0, int(round(float(remaining)))),
        "totalNetWeight": max(1, int(round(float(total)))),
    }


COMPARED_FIELDS = (
    "filamentVendor", "filamentType", "filamentName", "filamentId",
    "color", "netWeight", "totalNetWeight",
)


def cloud_field_diff(wanted: dict, cloud: dict) -> dict:
    """Fields of `wanted` that differ from the cloud spool."""
    diff: dict = {}
    for key in COMPARED_FIELDS:
        have, want = cloud.get(key), wanted[key]
        if key == "color":
            if hex6(have) != hex6(want):
                diff[key] = want
        elif key in ("netWeight", "totalNetWeight"):
            try:
                same = int(have) == want
            except (TypeError, ValueError):
                same = False
            if not same:
                diff[key] = want
        elif (have or "") != want:
            diff[key] = want
    return diff
