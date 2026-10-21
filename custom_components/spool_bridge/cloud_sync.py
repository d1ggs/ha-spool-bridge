"""Mirror Spoolman into the Bambu cloud filament library (Spoolman always wins)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

from .bambu_cloud import AuthExpired, BambuCloudError
from .const import CLOUD_WRITE_MIN_INTERVAL_S, EXTRA_CLOUD_ID, EXTRA_RFID, EXTRA_SLOT
from .model import SlotRef, cloud_field_diff, cloud_payload, extra_get, hex6

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Create:
    spool_id: int
    payload: dict


@dataclass(frozen=True)
class Update:
    spool_id: int
    cloud_id: int
    fields: dict


@dataclass(frozen=True)
class Delete:
    spool_id: int
    cloud_id: int


@dataclass(frozen=True)
class Link:
    spool_id: int
    cloud_id: int


@dataclass(frozen=True)
class Bind:
    cloud_id: int
    slot: SlotRef


@dataclass(frozen=True)
class Unbind:
    slot: SlotRef


def _cloud_slot(c: dict, dev_id: str) -> SlotRef | None:
    if not c.get("inPrinter") or c.get("devId") != dev_id or c.get("amsId") is None:
        return None
    try:
        return SlotRef("ams", int(c["amsId"]), int(c.get("slotId")))
    except (TypeError, ValueError):
        return None


def _orphan(cloud: list[dict], claimed: set[int], payload: dict) -> dict | None:
    """An unclaimed manual cloud spool with the same vendor, material and colour."""
    for c in sorted(cloud, key=lambda c: c.get("id", 0)):
        if (
            c.get("createType") == "manual" and c.get("id") not in claimed
            and c.get("filamentVendor") == payload["filamentVendor"]
            and c.get("filamentType") == payload["filamentType"]
            and hex6(c.get("color")) == hex6(payload["color"])
        ):
            return c
    return None


def plan(spools: list[dict], cloud: list[dict], dev_id: str) -> list[Any]:
    by_id = {c["id"]: c for c in cloud if isinstance(c.get("id"), int)}
    by_rfid = {str(c.get("RFID")).upper(): c for c in cloud if c.get("RFID")}
    actions: list[Any] = []
    wanted_slots: dict[int, SlotRef] = {}
    linked: set[int] = set()
    # Cloud ids already claimed by some Spoolman spool; anything else that looks
    # like one of ours (e.g. a create whose id was never saved) is adopted
    # instead of creating a duplicate.
    claimed = {int(v) for s in spools if (v := extra_get(s, EXTRA_CLOUD_ID)) and v.isdigit()}
    claimed |= {by_rfid[r.upper()]["id"] for s in spools
                if (r := extra_get(s, EXTRA_RFID)) and r.upper() in by_rfid}
    for spool in spools:
        sid = spool["id"]
        raw = extra_get(spool, EXTRA_CLOUD_ID)
        c = by_id.get(int(raw)) if raw and raw.isdigit() else None
        if spool.get("archived"):
            if c is not None:
                actions.append(Delete(sid, c["id"]))
            continue
        rfid = extra_get(spool, EXTRA_RFID)
        if c is None and rfid and (c := by_rfid.get(rfid.upper())) is not None:
            actions.append(Link(sid, c["id"]))
        if c is None and not rfid and (c := _orphan(cloud, claimed, cloud_payload(spool))) is not None:
            claimed.add(c["id"])
            actions.append(Link(sid, c["id"]))
        if c is None:
            if not rfid:  # RFID spools are registered by the AMS path, never created as manual
                actions.append(Create(sid, cloud_payload(spool)))
            continue
        linked.add(c["id"])
        if diff := cloud_field_diff(cloud_payload(spool), c):
            actions.append(Update(sid, c["id"], diff))
        slot = SlotRef.parse(extra_get(spool, EXTRA_SLOT))
        if slot is not None and slot.kind == "ams":
            wanted_slots[c["id"]] = slot
            # The printer owns RFID spool positions: the cloud rejects manual
            # binds (code 3) and unbinds (code 4, even on an empty slot), and
            # updates them itself once a print uses the spool or another loads.
            if _cloud_slot(c, dev_id) != slot and not c.get("RFID"):
                actions.append(Bind(c["id"], slot))
    taken = set(wanted_slots.values())
    for cid in linked:
        if by_id[cid].get("RFID"):
            continue
        current = _cloud_slot(by_id[cid], dev_id)
        if current is not None and cid not in wanted_slots and current not in taken:
            actions.append(Unbind(current))
    return actions


def ams_sync_item(tray: Any, state: Any, payload: dict) -> dict:
    """`/ams/sync` item that registers an RFID spool at its slot (Studio's AmsSyncItem)."""
    color = hex6(state.color) or hex6(payload["color"]) or "FFFFFF"
    return {
        "RFID": state.rfid, "createType": "ams",
        "filamentVendor": payload["filamentVendor"], "filamentType": payload["filamentType"],
        "filamentName": payload["filamentName"], "filamentId": state.filament_id or payload["filamentId"],
        "isSupport": "support" in payload["filamentName"].lower(),
        "color": f"#{color}", "colorType": 2, "colors": [f"#{color}FF"], "rolls": 1,
        "netWeight": payload["netWeight"], "totalNetWeight": payload["totalNetWeight"], "note": "",
        "amsSn": tray.ams_serial, "slotId": str(tray.slot.slot), "amsId": tray.slot.unit,
        "amsType": tray.ams_type,
    }


class CloudSync:
    def __init__(self, hass, client, spoolman, printer, dev_id: str) -> None:
        self.hass, self.client, self.spoolman, self.printer = hass, client, spoolman, printer
        self.dev_id = dev_id
        self.errors: list[str] = []
        self._last_write: dict[int, float] = {}

    async def _cloud(self, func, *args):
        return await self.hass.async_add_executor_job(func, *args)

    def _throttled(self, cloud_id: int) -> bool:
        return time.monotonic() - self._last_write.get(cloud_id, -1e9) < CLOUD_WRITE_MIN_INTERVAL_S

    async def reconcile(self) -> list[Any]:
        """One full pass. Raises AuthExpired; other errors are collected in `errors`."""
        self.errors = []
        spools = await self.spoolman.get_spools(allow_archived=True)
        cloud = await self._cloud(self.client.get_spools)
        cloud_by_id = {c["id"]: c for c in cloud}
        done: list[Any] = []
        for action in plan(spools, cloud, self.dev_id):
            try:
                if await self._apply(action, cloud_by_id):
                    done.append(action)
            except AuthExpired:
                raise
            except BambuCloudError as err:
                self.errors.append(f"{type(action).__name__}: {err}")
        return done

    async def _apply(self, action: Any, cloud_by_id: dict[int, dict]) -> bool:
        if isinstance(action, Create):
            known = set(cloud_by_id)
            # The create endpoint rejects bodies without createType (HTTP 400,
            # verified live); colorType/isSupport match what Studio sends.
            body = {"createType": "manual", "colorType": 2, "isSupport": False, **action.payload}
            await self._cloud(self.client.create_spool, body)
            fresh = [c for c in await self._cloud(self.client.get_spools)
                     if c["id"] not in known and c.get("filamentName") == action.payload["filamentName"]
                     and hex6(c.get("color")) == hex6(action.payload["color"])]
            if not fresh:
                self.errors.append(f"Create: new cloud spool for {action.spool_id} not found")
                return False
            new_id = max(c["id"] for c in fresh)
            cloud_by_id[new_id] = next(c for c in fresh if c["id"] == new_id)
            await self.spoolman.set_spool_extra(action.spool_id, {EXTRA_CLOUD_ID: str(new_id)})
            return True
        if isinstance(action, Update):
            if self._throttled(action.cloud_id):
                return False
            body = {"id": action.cloud_id,
                    "filamentName": action.fields.get("filamentName") or cloud_by_id[action.cloud_id].get("filamentName") or "",
                    **action.fields}
            if "color" in action.fields:
                body["colors"] = [action.fields["color"]]
            await self._cloud(self.client.update_spool, body)
            self._last_write[action.cloud_id] = time.monotonic()
            return True
        if isinstance(action, Delete):
            await self._cloud(self.client.delete_spools, [action.cloud_id])
            await self.spoolman.set_spool_extra(action.spool_id, {EXTRA_CLOUD_ID: None})
            return True
        if isinstance(action, Link):
            await self.spoolman.set_spool_extra(action.spool_id, {EXTRA_CLOUD_ID: str(action.cloud_id)})
            return True
        if isinstance(action, (Bind, Unbind)):
            tray = self.printer.tray_for(action.slot)
            if tray is None or not tray.ams_serial:
                self.errors.append(f"{type(action).__name__}: no AMS tray for {action.slot.key}")
                return False
            if isinstance(action, Unbind):
                state = self.printer.tray_state(tray)
                if state is None or not state.empty:
                    return False  # the cloud only accepts unbinding an empty slot
            mapping = {"amsId": action.slot.unit, "amsSn": tray.ams_serial, "amsType": tray.ams_type,
                       "rfid": "",
                       "slotId": str(action.slot.slot),
                       "spoolId": action.cloud_id if isinstance(action, Bind) else 0}
            results = await self._cloud(self.client.slot_mappings_sync, self.dev_id, [mapping])
            codes = [r.get("code") for r in results]
            if codes != [0]:
                self.errors.append(f"{type(action).__name__} {action.slot.key}: cloud code {codes[0] if codes else 'none'}")
                return False
            return True
        return False

    async def register_rfid(self, tray: Any, state: Any, spool: dict) -> None:
        """Create the cloud entry for an RFID spool the cloud does not know yet."""
        item = ams_sync_item(tray, state, cloud_payload(spool))
        await self._cloud(self.client.ams_sync, self.dev_id, [item])
