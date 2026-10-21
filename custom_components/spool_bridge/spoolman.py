"""Async client for the Spoolman REST API and change websocket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

import aiohttp

from .const import EXTRA_CLOUD_ID, EXTRA_FILAMENT_ID, EXTRA_RFID, EXTRA_SLOT
from .model import extra_value, hex6

_LOGGER = logging.getLogger(__name__)

KEEP = object()

EXTRA_FIELDS = (
    ("spool", EXTRA_CLOUD_ID, "Bambu cloud id"),
    ("spool", EXTRA_RFID, "Bambu RFID"),
    ("spool", EXTRA_SLOT, "Bambu slot"),
    ("filament", EXTRA_FILAMENT_ID, "Bambu filament profile id"),
)
DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21}


class SpoolmanError(Exception):
    """Spoolman unreachable or returned an error."""


class SpoolmanClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")

    async def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}/api/v1{path}"
        try:
            async with self._session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=15), **kwargs
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    raise SpoolmanError(f"{method} {path}: HTTP {resp.status}: {await resp.text()}")
                if resp.status == 204:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SpoolmanError(f"{method} {path}: {err}") from err

    async def info(self) -> dict:
        result = await self._req("GET", "/info")
        if not isinstance(result, dict):
            raise SpoolmanError("Not a Spoolman server")
        return result

    async def ensure_extra_fields(self) -> None:
        for entity, key, name in EXTRA_FIELDS:
            existing = await self._req("GET", f"/field/{entity}") or []
            if not any(f.get("key") == key for f in existing):
                await self._req("POST", f"/field/{entity}/{key}", json={"name": name, "field_type": "text"})

    async def get_spools(self, allow_archived: bool = True) -> list[dict]:
        params = {"allow_archived": "true" if allow_archived else "false"}
        return list(await self._req("GET", "/spool", params=params) or [])

    async def get_spool(self, spool_id: int) -> dict | None:
        return await self._req("GET", f"/spool/{int(spool_id)}")

    async def patch_spool(self, spool_id: int, body: dict) -> dict:
        result = await self._req("PATCH", f"/spool/{int(spool_id)}", json=body)
        if result is None:
            raise SpoolmanError(f"Spool {spool_id} not found")
        return result

    async def set_spool_extra(
        self, spool_id: int, values: dict[str, str | None], location: Any = KEEP
    ) -> dict:
        """Set/remove only the given extra keys (Spoolman merges per key)."""
        body: dict[str, Any] = {"extra": {k: extra_value(v) for k, v in values.items()}}
        if location is not KEEP:
            body["location"] = location
        return await self.patch_spool(spool_id, body)

    async def get_filaments(self) -> list[dict]:
        return list(await self._req("GET", "/filament") or [])

    async def patch_filament(self, filament_id: int, body: dict) -> dict:
        result = await self._req("PATCH", f"/filament/{int(filament_id)}", json=body)
        if result is None:
            raise SpoolmanError(f"Filament {filament_id} not found")
        return result

    async def use_weight(self, spool_id: int, grams: float) -> dict:
        result = await self._req("PUT", f"/spool/{int(spool_id)}/use", json={"use_weight": float(grams)})
        if result is None:
            raise SpoolmanError(f"Spool {spool_id} not found")
        return result

    async def ensure_vendor(self, name: str) -> int:
        for vendor in await self._req("GET", "/vendor") or []:
            if (vendor.get("name") or "").strip().lower() == name.strip().lower():
                return vendor["id"]
        return (await self._req("POST", "/vendor", json={"name": name}))["id"]

    async def ensure_filament(
        self, vendor_id: int, *, name: str, material: str, color_hex: str,
        weight: float, spool_weight: float | None, bambu_filament_id: str | None,
    ) -> int:
        color = hex6(color_hex)
        for fil in await self._req("GET", "/filament") or []:
            if (
                (fil.get("vendor") or {}).get("id") == vendor_id
                and (fil.get("name") or "") == name
                and (fil.get("material") or "") == material
                and hex6(fil.get("color_hex")) == color
            ):
                return fil["id"]
        body = {
            "vendor_id": vendor_id, "name": name, "material": material,
            "color_hex": color or None, "weight": weight, "spool_weight": spool_weight,
            "density": DENSITY.get(material.upper(), 1.24), "diameter": 1.75,
            "extra": {EXTRA_FILAMENT_ID: extra_value(bambu_filament_id)} if bambu_filament_id else {},
        }
        return (await self._req("POST", "/filament", json=body))["id"]

    async def create_spool(
        self, filament_id: int, *, initial_weight: float, remaining_weight: float,
        extra: dict[str, str | None], location: str | None,
    ) -> dict:
        body = {
            "filament_id": filament_id, "initial_weight": initial_weight,
            "remaining_weight": remaining_weight, "location": location,
            "extra": {k: extra_value(v) for k, v in extra.items() if v is not None},
        }
        return await self._req("POST", "/spool", json=body)

    async def listen(self, on_event: Callable[[dict], None]) -> None:
        """Forward spool change events until cancelled; reconnect on errors."""
        ws_url = self._base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/v1/spool"
        while True:
            try:
                async with self._session.ws_connect(ws_url, heartbeat=30) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                on_event(msg.json())
                            except Exception:  # noqa: BLE001 - one bad event must not end the stream
                                _LOGGER.exception("Spoolman event handling failed")
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Spoolman websocket error: %s", err)
            await asyncio.sleep(10)
