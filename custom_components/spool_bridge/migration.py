"""One-time migration: import Bambu RFID cloud spools into Spoolman, delete manual ones."""

from __future__ import annotations

from typing import Any

from .const import EXTRA_CLOUD_ID, EXTRA_RFID, EXTRA_SLOT
from .model import SlotRef, extra_get, hex6


def plan_migration(cloud: list[dict]) -> tuple[list[dict], list[dict]]:
    imports = [c for c in cloud if c.get("createType") == "ams" and c.get("RFID")]
    deletes = [c for c in cloud if c.get("createType") == "manual"]
    return imports, deletes


def _line(c: dict) -> str:
    return f"- {c.get('filamentVendor')} {c.get('filamentName')} #{hex6(c.get('color'))} ({c.get('netWeight')} g)"


def describe(imports: list[dict], deletes: list[dict]) -> str:
    parts = [f"Import {len(imports)} Bambu RFID spool(s) into Spoolman:", *map(_line, imports),
             "", f"Delete {len(deletes)} other spool(s) from the Bambu cloud:", *map(_line, deletes)]
    return "\n".join(parts)


async def import_spools(spoolman: Any, imports: list[dict], dev_id: str, printer_name: str) -> int:
    linked = {extra_get(s, EXTRA_CLOUD_ID) for s in await spoolman.get_spools(allow_archived=True)}
    created = 0
    for c in imports:
        if str(c["id"]) in linked:
            continue
        vendor = await spoolman.ensure_vendor(c.get("filamentVendor") or "Bambu Lab")
        total = float(c.get("totalNetWeight") or 1000)
        filament = await spoolman.ensure_filament(
            vendor, name=c.get("filamentName") or c.get("filamentType") or "PLA",
            material=c.get("filamentType") or "PLA", color_hex=hex6(c.get("color")),
            weight=total, spool_weight=250, bambu_filament_id=c.get("filamentId") or None,
        )
        slot = None
        if c.get("inPrinter") and c.get("devId") == dev_id and c.get("amsId") is not None:
            slot = SlotRef("ams", int(c["amsId"]), int(c["slotId"]))
        await spoolman.create_spool(
            filament, initial_weight=total, remaining_weight=float(c.get("netWeight") or 0),
            extra={EXTRA_CLOUD_ID: str(c["id"]), EXTRA_RFID: str(c["RFID"]).upper(),
                   EXTRA_SLOT: slot.key if slot else None},
            location=slot.label(printer_name) if slot else None,
        )
        created += 1
    return created
