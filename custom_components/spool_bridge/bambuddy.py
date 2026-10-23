"""Async client for the Bambuddy slot-assignment API (Spoolman mode)."""

from __future__ import annotations

import json
from typing import Any

import aiohttp

from .model import SlotRef, bambuddy_slot, slot_from_bambuddy

_ASSIGNMENTS = "/api/v1/spoolman/inventory/slot-assignments"


class BambuddyError(Exception):
    """Bambuddy unreachable, refused the API key or returned an error."""


class BambuddyClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}

    async def _req(self, method: str, path: str, *, ok_404: bool = False, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method, f"{self._base}{path}", headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15), **kwargs,
            ) as resp:
                if resp.status == 404 and ok_404:
                    return None
                text = await resp.text()
                if resp.status >= 400:
                    raise BambuddyError(f"{method} {path}: HTTP {resp.status}: {text[:200]}")
                return json.loads(text) if text else None
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise BambuddyError(f"{method} {path}: {err}") from err

    async def printer_id(self, serial: str) -> int:
        for printer in await self._req("GET", "/api/v1/printers/") or []:
            if printer.get("serial_number") == serial:
                return int(printer["id"])
        raise BambuddyError("The printer is not set up in Bambuddy")

    async def assignments(self, printer_id: int) -> dict[SlotRef, int]:
        """Bambuddy's spool per slot of this printer (AMS HT slots left out)."""
        rows = await self._req("GET", f"{_ASSIGNMENTS}/all", params={"printer_id": printer_id}) or []
        found: dict[SlotRef, int] = {}
        for row in rows:
            slot = slot_from_bambuddy(int(row["ams_id"]), int(row["tray_id"]))
            if slot is not None:
                found[slot] = int(row["spoolman_spool_id"])
        return found

    async def assign(self, printer_id: int, spool_id: int, slot: SlotRef) -> None:
        ams_id, tray_id = bambuddy_slot(slot)
        await self._req("POST", _ASSIGNMENTS, json={
            "spoolman_spool_id": spool_id, "printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id})

    async def unassign(self, spool_id: int) -> None:
        await self._req("DELETE", f"{_ASSIGNMENTS}/{int(spool_id)}", ok_404=True)

    async def nudge_print_file(self, printer_id: int) -> None:
        """Ask for the running print's cover: Bambuddy downloads the print's 3MF
        for it and fills in the print's archive if it started without one."""
        try:
            async with self._session.get(
                f"{self._base}/api/v1/printers/{int(printer_id)}/cover", headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BambuddyError(f"cover: {err}") from err
