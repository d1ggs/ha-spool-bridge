"""Wires HA events, Spoolman, the printer and Bambuddy together."""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .assignment import Ambiguous, Assign, Expired, LoadWindow
from .bambuddy import BambuddyClient, BambuddyError
from .const import (
    ACTION_PREFIX, BAMBUDDY_SYNC_INTERVAL, BUSY_STATES, CONF_NAME_RULES, CONF_NOTIFY_SERVICE, DOMAIN, END_STATES,
    EXTRA_RFID, EXTRA_SLICER_FILAMENT, EXTRA_SLOT, EXTRA_TAG,
    IDLE_STATES, JOB_FETCH_ATTEMPTS, JOB_FETCH_DELAY_S, LEDGER_RETRY_INTERVAL, LOAD_WINDOW_S,
    RECONCILE_DEBOUNCE_S, RECONCILE_INTERVAL, RUNOUT_PAUSE_GRACE_S, SET_FILAMENT_ATTEMPTS, SET_FILAMENT_RETRY_DELAY_S,
    SETTLE_S, SIGNAL_STATE, STUCK_AFTER_S, TASK_FAILED, TASK_FINISHED,
)
from .model import SlotRef, extra_get, hex6, slot_from_flat, spool_filament_id, tag_to_spool_id
from .naming import RuleError, parse_rules, renamed
from .printer import Printer, TrayState, parse_tray_state
from .spoolman import SpoolmanError
from .spoolman_proxy import SpoolmanProxy
from .usage import Entry, Ledger, apply_entry, plan_entries, runout_candidates

_LOGGER = logging.getLogger(__name__)


def _reading(state: TrayState) -> bool:
    """ha-bambulab shows a tray as "?" while the AMS reads a freshly loaded spool."""
    return not state.empty and state.material == "?"


class Bridge:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, spoolman, printer: Printer,
                 bambuddy: BambuddyClient | None = None, proxy: SpoolmanProxy | None = None) -> None:
        self.hass, self.entry, self.spoolman, self.printer = hass, entry, spoolman, printer
        # Bambuddy in Spoolman mode: slot assignments kept in step both ways; it
        # reads Spoolman through `proxy`, which keeps the bridge the usage owner.
        self.bambuddy, self.proxy = bambuddy, proxy
        self.bambuddy_printer: int | None = None
        self._bb_seen: dict[SlotRef, int] | None = None  # Bambuddy's table as last read/written
        self._bb_lock = asyncio.Lock()
        self._usage_ignored: deque[tuple[float, int, float]] = deque(maxlen=20)
        self.dev_id = printer.serial
        self.window = LoadWindow(LOAD_WINDOW_S, SETTLE_S)
        self.ledger = Ledger()
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._unsubs: list = []
        self._tasks: list[asyncio.Task] = []
        self._reconcile_handle: asyncio.TimerHandle | None = None
        self._reconcile_lock = asyncio.Lock()
        self._ledger_lock = asyncio.Lock()
        self._stuck_notified = False
        self._fetching = False
        self.state: dict[str, Any] = {"pending_jobs": 0, "last_error": None}

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.ledger = Ledger.from_dict(stored.get("ledger") or {})
        if self.ledger.last_end_ts is None:
            self.ledger.last_end_ts = time.time()  # never book history from before install
            await self._save()
        trays = [t.entity_id for t in self.printer.trays()]
        self._unsubs += [
            self.hass.bus.async_listen("tag_scanned", self._tag_event),
            self.hass.bus.async_listen("mobile_app_notification_action", self._action_event),
            async_track_state_change_event(self.hass, trays, self._tray_event),
            async_track_time_interval(self.hass, self._interval_reconcile, RECONCILE_INTERVAL),
            async_track_time_interval(self.hass, self._interval_ledger, LEDGER_RETRY_INTERVAL),
        ]
        watched = [e for e in (self.printer.status_entity, self.printer.progress_entity,
                               self.printer.weight_entity) if e]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._printer_event))
        if self.proxy is not None:
            try:
                await self.proxy.start()
            except OSError as err:
                self._error(f"Spoolman gateway for Bambuddy did not start on {self.proxy.url}: {err}")
        if self.bambuddy is not None:
            self._unsubs.append(async_track_time_interval(self.hass, self._interval_bambuddy, BAMBUDDY_SYNC_INTERVAL))
        self._tasks.append(self.entry.async_create_background_task(
            self.hass, self.spoolman.listen(self._spoolman_event), f"{DOMAIN} spoolman websocket"))
        self._tasks.append(self.entry.async_create_background_task(
            self.hass, self._ticker(), f"{DOMAIN} ticker"))
        self._tasks.append(self.entry.async_create_background_task(
            self.hass, self._startup(), f"{DOMAIN} startup"))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._reconcile_handle:
            self._reconcile_handle.cancel()
        if self.proxy is not None:
            await self.proxy.stop()

    async def _startup(self) -> None:
        await self.sync_rfid_trays()
        await self.catch_up_jobs()
        await self.request_reconcile(now=True)
        await self.sync_from_bambuddy()

    async def _save(self) -> None:
        self.state["pending_jobs"] = len(self.ledger.pending())
        await self._store.async_save({"ledger": self.ledger.to_dict()})
        async_dispatcher_send(self.hass, SIGNAL_STATE)

    def _error(self, message: str) -> None:
        _LOGGER.warning(message)
        self.state["last_error"] = message
        async_dispatcher_send(self.hass, SIGNAL_STATE)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self._safe_tick()

    async def _safe_tick(self) -> None:
        """One tick; an unexpected error must never stop the scan -> load loop."""
        try:
            await self.async_tick()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Spool Bridge tick failed")

    async def async_tick(self) -> None:
        now = time.time()
        decision = self.window.settle(now) or self.window.expire(now)
        if isinstance(decision, Assign):
            await self.assign(decision.spool_id, decision.slot)
        elif isinstance(decision, Ambiguous):
            await self._ask_slot(decision.spool_id, decision.slots, "More than one slot changed. Which one?")
        elif isinstance(decision, Expired):
            await self._ask_slot(decision.spool_id, tuple(t.slot for t in self.printer.trays()),
                                 "No slot change seen. Pick the slot:")

    # ------------------------------------------------------------ notifications

    async def notify(self, message: str, actions: list[dict] | None = None) -> None:
        data: dict[str, Any] = {"title": "Spool Bridge", "message": message}
        if actions:
            data["data"] = {"actions": actions[:10]}
        try:
            await self.hass.services.async_call("notify", self.entry.data[CONF_NOTIFY_SERVICE], data, blocking=False)
        except Exception as err:  # noqa: BLE001 - a missing phone must never break the bridge
            _LOGGER.warning("Notification failed: %s", err)

    async def _ask_slot(self, spool_id: int, slots: tuple[SlotRef, ...], message: str) -> None:
        name = self.printer.name()
        actions = [{"action": f"{ACTION_PREFIX}|{spool_id}|{s.key}", "title": s.label(name)} for s in slots]
        await self.notify(message, actions)

    @callback
    def _action_event(self, event: Event) -> None:
        parts = str(event.data.get("action") or "").split("|")
        if len(parts) == 3 and parts[0] == ACTION_PREFIX and parts[1].isdigit() and (slot := SlotRef.parse(parts[2])):
            self.hass.async_create_task(self.assign(int(parts[1]), slot))

    # ------------------------------------------------------------ scan -> load

    @callback
    def _tag_event(self, event: Event) -> None:
        self.hass.async_create_task(self.on_tag(event.data.get("tag_id")))

    async def on_tag(self, tag_id: str | None) -> None:
        spool_id = tag_to_spool_id(tag_id)
        if spool_id is None:
            return
        spool = await self._spool(spool_id)
        if spool is None or spool.get("archived"):
            await self.notify(f"Unknown spool {spool_id}")
            return
        self.window.scan(spool_id, time.time())
        fil = spool.get("filament") or {}
        await self.notify(f"{fil.get('name') or fil.get('material')}, {round(spool.get('remaining_weight') or 0)} g"
                          " — load it into any slot within 5 min.")

    async def _spool(self, spool_id: int) -> dict | None:
        try:
            return await self.spoolman.get_spool(spool_id)
        except SpoolmanError as err:
            self._error(f"Spoolman: {err}")
            return None

    @callback
    def _tray_event(self, event: Event) -> None:
        self.hass.async_create_task(self.on_tray_change(
            event.data["entity_id"], event.data.get("old_state"), event.data.get("new_state")))

    async def on_tray_change(self, entity_id: str, old_state, new_state) -> None:
        tray = self.printer.tray_by_entity(entity_id)
        old, new = parse_tray_state(old_state), parse_tray_state(new_state)
        # External holders report "empty" whenever they are not the active feed
        # (ha-bambulab), so their transitions say nothing about loading.
        if tray is None or tray.slot.kind != "ams" or new is None:
            return
        if old is None:
            # Back from unavailable (HA or ha-bambulab restart): the startup sync ran
            # before the trays were known, so an RFID spool may still lack its slot.
            if new.rfid and not new.empty:
                await self.sync_rfid_trays()
            return
        if new.active and not old.active:
            await self._confirm_backup(tray.slot)
        if new.empty:
            if not old.empty:
                await self._record_runout(tray.slot)
                await self.unassign(tray.slot)
            return
        # "?" (not empty, no tag yet) while the AMS reads the spool: decide once it settles.
        if _reading(new):
            return
        if not old.empty and not _reading(old):
            if new.rfid and new.rfid != old.rfid:  # a different RFID spool without an empty in between
                await self.sync_rfid_trays()
            return
        if new.rfid:
            await self.sync_rfid_trays()
            return
        self.window.tray_loaded(tray.slot, time.time())

    # ------------------------------------------------------------ auto-refill

    def _runouts(self) -> dict[str, dict] | None:
        """Runouts of the print in progress (None when no print is being tracked)."""
        snapshot = self.ledger.snapshot
        if snapshot is None or snapshot.get("booked") or self.printer.status() != "running":
            return None
        return snapshot.setdefault("runouts", {})

    async def _record_runout(self, slot: SlotRef) -> None:
        """A tray of the running print went empty. If the print carries on, the printer switched to a
        same-filament tray (AMS auto-refill) and the print still reports all grams on this slot."""
        runouts = self._runouts()
        progress = self.ledger.progress if self.ledger.progress is not None else self.printer.progress()
        if runouts is None or progress is None or slot.key not in self.ledger.snapshot["assignment"]:
            return
        assignment = self.ledger.snapshot["assignment"]
        try:
            spools = {s["id"]: s for s in await self.spoolman.get_spools(allow_archived=False)}
        except SpoolmanError as err:
            self._error(f"Runout on {slot.key}: Spoolman unreachable, usage will not be split ({err})")
            return
        # only AMS trays can be auto-refilled from; external holders report "empty" when not feeding
        states = {t.slot.key: st for t in self.printer.trays()
                  if t.slot.kind == "ams" and (st := self.printer.tray_state(t)) is not None}
        candidates = runout_candidates(slot.key, assignment, spools, {k for k, st in states.items() if not st.empty})
        if not candidates:
            return  # nothing to split with: book as before
        active = next((k for k in candidates if states[k].active), None)
        runouts[slot.key] = {"progress": progress, "backup": active or candidates[0], "candidates": candidates,
                             "confirmed": active is not None, "ts": time.time()}
        await self._save()

    async def _confirm_backup(self, slot: SlotRef) -> None:
        """The printer now feeds from `slot`: that is the backup of any runout it could have refilled."""
        runouts = self._runouts() or {}
        # a runout already confirmed, or whose backup ran dry too (chained), is left alone
        hit = [r for r in runouts.values() if slot.key in r["candidates"] and not r["confirmed"]
               and r["backup"] not in runouts and slot.key not in runouts]
        for run in hit:
            run["backup"], run["confirmed"] = slot.key, True
        if hit:
            await self._save()

    async def assign(self, spool_id: int, slot: SlotRef, *, from_bambuddy: bool = False) -> None:
        try:
            spools = await self.spoolman.get_spools(allow_archived=False)
            spool = next((s for s in spools if s["id"] == spool_id), None)
            if spool is None:
                await self.notify(f"Unknown spool {spool_id}")
                return
            for other in spools:
                if other["id"] != spool_id and extra_get(other, EXTRA_SLOT) == slot.key:
                    await self.spoolman.set_spool_extra(other["id"], {EXTRA_SLOT: None}, location=None)
            name = self.printer.name()
            await self.spoolman.set_spool_extra(spool_id, {EXTRA_SLOT: slot.key}, location=slot.label(name))
        except SpoolmanError as err:
            self._error(f"Assign failed: {err}")
            await self.notify(f"Could not assign spool {spool_id}: Spoolman unreachable")
            return
        if from_bambuddy:
            # Assigned in Bambuddy's UI, which has already set the tray profile.
            await self.request_reconcile(now=True)
            return
        fil = spool.get("filament") or {}
        tray = self.printer.tray_for(slot)
        profile_error: Exception | None = None
        # Without Developer mode the firmware refuses control commands: the write
        # would only time out, so skip it and say what to set by hand.
        control_blocked = self.printer.control_blocked()
        if tray is not None and not extra_get(spool, EXTRA_RFID) and not control_blocked:
            for attempt in range(SET_FILAMENT_ATTEMPTS):
                try:
                    await self.printer.set_filament(
                        tray, filament_id=spool_filament_id(spool), color_hex=fil.get("color_hex"),
                        material=fil.get("material"), nozzle_temp=fil.get("settings_extruder_temp"))
                    profile_error = None
                    break
                except Exception as err:  # noqa: BLE001 - retried, then reported
                    profile_error = err
                    if attempt + 1 < SET_FILAMENT_ATTEMPTS:
                        await asyncio.sleep(SET_FILAMENT_RETRY_DELAY_S)
        await self._push_to_bambuddy(spool, slot)
        await self.request_reconcile(now=True)
        label = f"{fil.get('name') or fil.get('material')} → {slot.label(name)}"
        if profile_error is not None:
            self._error(f"set_filament {slot.key}: {profile_error}")
            await self.notify(f"{label}: assigned, but the printer profile could not be set ({profile_error}).")
        elif control_blocked and not extra_get(spool, EXTRA_RFID):
            await self.notify(
                f"{label} ✓ — the printer refuses profile changes from Home Assistant (it needs "
                f"LAN-only + Developer mode): set this tray's filament to "
                f"{fil.get('name') or fil.get('material')} ({spool_filament_id(spool)}) on the printer screen.")
        else:
            await self.notify(f"{label} ✓")

    async def unassign(self, slot: SlotRef, *, from_bambuddy: bool = False) -> None:
        cleared: list[int] = []
        try:
            for spool in await self.spoolman.get_spools(allow_archived=False):
                if extra_get(spool, EXTRA_SLOT) == slot.key:
                    await self.spoolman.set_spool_extra(spool["id"], {EXTRA_SLOT: None}, location=None)
                    cleared.append(spool["id"])
        except SpoolmanError as err:
            self._error(f"Unassign failed: {err}")
            return
        if not from_bambuddy:
            for spool_id in cleared:
                await self._bambuddy_unassign(spool_id)
        await self.request_reconcile(now=True)

    async def sync_rfid_trays(self) -> None:
        """Every loaded RFID tray has a Spoolman spool assigned to it (created if needed)."""
        try:
            spools = await self.spoolman.get_spools(allow_archived=False)
            if self.bambuddy is not None:
                await self._sync_rfid_tags(spools)
            for tray in self.printer.trays():
                state = self.printer.tray_state(tray)
                if tray.slot.kind != "ams" or state is None or state.empty or not state.rfid:
                    continue
                spool = next((s for s in spools if (extra_get(s, EXTRA_RFID) or "").upper() == state.rfid), None)
                if spool is None:
                    vendor = await self.spoolman.ensure_vendor("Bambu Lab")
                    total = float(state.tray_weight or 1000)
                    filament = await self.spoolman.ensure_filament(
                        vendor, name=state.name.removeprefix("Bambu ").strip() or state.material,
                        material=state.material or "PLA", color_hex=hex6(state.color), weight=total,
                        spool_weight=250, bambu_filament_id=state.filament_id or None)
                    remaining = total * state.remain / 100 if state.remain >= 0 else total
                    extra = {EXTRA_RFID: state.rfid}
                    if self.bambuddy is not None:
                        extra[EXTRA_TAG] = state.rfid
                    spool = await self.spoolman.create_spool(
                        filament, initial_weight=total, remaining_weight=remaining, extra=extra, location=None)
                    spools.append(spool)
                if extra_get(spool, EXTRA_SLOT) != tray.slot.key:
                    await self.assign(spool["id"], tray.slot)
        except SpoolmanError as err:
            self._error(f"RFID sync: {err}")

    # ------------------------------------------------------------ Bambuddy (Spoolman mode)

    async def _sync_rfid_tags(self, spools: list[dict]) -> None:
        """Bambuddy links an RFID tray to the Spoolman spool whose `tag` holds the
        tray UUID; without it, it would offer to add the spool again."""
        for spool in spools:
            rfid = extra_get(spool, EXTRA_RFID)
            if rfid and (extra_get(spool, EXTRA_TAG) or "").upper() != rfid.upper():
                await self.spoolman.set_spool_extra(spool["id"], {EXTRA_TAG: rfid})

    async def _bambuddy_ready(self) -> bool:
        if self.bambuddy is None:
            return False
        if self.bambuddy_printer is None:
            try:
                self.bambuddy_printer = await self.bambuddy.printer_id(self.printer.serial)
            except BambuddyError as err:
                self._error(f"Bambuddy: {err}")
                return False
        return True

    def _slots_in_use(self) -> set[SlotRef] | None:
        """Slots the running print draws from (None: printing, but unknown which)."""
        if self.printer.status() not in BUSY_STATES:
            return set()
        weights = (self.ledger.snapshot or {}).get("weights")
        if not weights:
            return None
        return {slot for flat in weights if (slot := slot_from_flat(int(flat))) is not None}

    def _rfid_at(self, slot: SlotRef) -> bool:
        tray = self.printer.tray_for(slot)
        state = self.printer.tray_state(tray) if tray is not None else None
        return bool(state and not state.empty and state.rfid)

    async def _push_to_bambuddy(self, spool: dict, slot: SlotRef) -> None:
        if extra_get(spool, EXTRA_RFID) or not await self._bambuddy_ready():
            return
        async with self._bb_lock:
            await self._bb_push(spool, slot)

    async def _bb_push(self, spool: dict, slot: SlotRef) -> bool:
        """Show a bridge assignment on Bambuddy's slot (caller holds _bb_lock).

        RFID spools are linked by tag instead: a Bambuddy assignment sends a tray
        profile, which would replace the one the printer read from the tag."""
        if extra_get(spool, EXTRA_RFID):
            return False
        try:
            if extra_get(spool, EXTRA_SLICER_FILAMENT) is None:
                # Bambuddy derives the profile it sends from this per-spool preset;
                # the bridge's profile keeps both pushes identical.
                await self.spoolman.set_spool_extra(spool["id"], {EXTRA_SLICER_FILAMENT: spool_filament_id(spool)})
            await self.bambuddy.unassign(spool["id"])  # a spool sits in one slot
            await self.bambuddy.assign(self.bambuddy_printer, spool["id"], slot)
        except (BambuddyError, SpoolmanError) as err:
            self._error(f"Bambuddy assign {slot.key}: {err}")
            return False
        if self._bb_seen is not None:
            self._bb_seen = {s: i for s, i in self._bb_seen.items() if i != spool["id"]}
            self._bb_seen[slot] = spool["id"]
        return True

    async def _bambuddy_unassign(self, spool_id: int) -> None:
        if not await self._bambuddy_ready():
            return
        async with self._bb_lock:
            try:
                await self.bambuddy.unassign(spool_id)
            except BambuddyError as err:
                self._error(f"Bambuddy unassign: {err}")
                return
            if self._bb_seen is not None:
                self._bb_seen = {s: i for s, i in self._bb_seen.items() if i != spool_id}

    async def _interval_bambuddy(self, _now=None) -> None:
        await self.sync_from_bambuddy()

    async def sync_from_bambuddy(self) -> None:
        """Keep Bambuddy's slot table and Spoolman's slots in step.

        A change on Bambuddy's side since the last look (an assignment made or
        removed in its UI) is applied to Spoolman; a bridge assignment Bambuddy
        lacks is pushed to it. On the first look (startup) Spoolman wins any
        disagreement. RFID trays are the bridge's alone."""
        if not await self._bambuddy_ready():
            return
        async with self._bb_lock:
            try:
                remote = await self.bambuddy.assignments(self.bambuddy_printer)
                spools = await self.spoolman.get_spools(allow_archived=False)
            except (BambuddyError, SpoolmanError) as err:
                self._error(f"Bambuddy sync: {err}")
                return
            by_id = {s["id"]: s for s in spools}
            local: dict[SlotRef, int] = {}
            for spool in spools:
                slot = SlotRef.parse(extra_get(spool, EXTRA_SLOT))
                if slot is not None and not extra_get(spool, EXTRA_RFID):
                    local[slot] = spool["id"]
            seen = self._bb_seen
            for slot, spool_id in remote.items():
                if local.get(slot) == spool_id or spool_id not in by_id or self._rfid_at(slot):
                    continue
                if seen is None and (slot in local or spool_id in local.values()):
                    continue  # first look: Spoolman's side is pushed below
                if seen is not None and seen.get(slot) == spool_id:
                    continue  # Bambuddy unchanged: the bridge moved on, pushed below
                await self.assign(spool_id, slot, from_bambuddy=True)
                local = {s: i for s, i in local.items() if i != spool_id}
                local[slot] = spool_id
            for slot, spool_id in (seen or {}).items():
                if slot not in remote and local.get(slot) == spool_id:
                    await self.unassign(slot, from_bambuddy=True)
                    del local[slot]
            self._bb_seen = dict(remote)
            # A Bambuddy assignment sends the slot's profile to the printer: leave
            # trays a running print draws from until it ends.
            in_use = self._slots_in_use()
            for slot, spool_id in local.items():
                if remote.get(slot) != spool_id and in_use is not None and slot not in in_use:
                    await self._bb_push(by_id[spool_id], slot)

    @callback
    def on_bambuddy_usage(self, spool_id: int, grams: float) -> None:
        """Bambuddy tried to book usage through the Spoolman gateway; the bridge books it."""
        self._usage_ignored.append((time.time(), spool_id, grams))
        _LOGGER.info("Bambuddy's %.2f g on spool %s not applied: the bridge books print usage", grams, spool_id)

    # ------------------------------------------------------------ filament names

    @callback
    def _spoolman_event(self, event: Any) -> None:
        if isinstance(event, dict) and event.get("resource") in ("spool", "filament", "vendor"):
            self.hass.async_create_task(self.request_reconcile())

    async def _interval_reconcile(self, _now=None) -> None:
        await self.sync_rfid_trays()  # heals a slot missed by an event (restart, odd AMS sequence)
        await self.request_reconcile(now=True)

    async def request_reconcile(self, now: bool = False) -> None:
        if self._reconcile_handle:
            self._reconcile_handle.cancel()
        if now:
            await self._reconcile()
            return
        self._reconcile_handle = self.hass.loop.call_later(
            RECONCILE_DEBOUNCE_S, lambda: self.hass.async_create_task(self._reconcile()))

    async def _reconcile(self) -> None:
        async with self._reconcile_lock:
            try:
                await self.rename_filaments()
            except SpoolmanError as err:
                self._error(f"Filament names: {err}")

    async def rename_filaments(self) -> None:
        """Apply the optional name rules to filaments imported from the external database.

        Filaments the bridge created itself are skipped: it finds them again by name.
        """
        try:
            rules = parse_rules(self.entry.options.get(CONF_NAME_RULES, ""))
        except RuleError as err:
            self._error(f"Filament name rules: {err}")
            return
        if not rules:
            return
        for filament in await self.spoolman.get_filaments():
            if not filament.get("external_id"):
                continue
            if new := renamed(filament.get("name") or "", rules):
                await self.spoolman.patch_filament(filament["id"], {"name": new})
                _LOGGER.info("Renamed filament %s: %r -> %r", filament["id"], filament.get("name"), new)

    # ------------------------------------------------------------ usage

    @callback
    def _printer_event(self, event: Event) -> None:
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if new is None:
            return
        if event.data["entity_id"] == self.printer.weight_entity:
            # ha-bambulab can split the grams per tray only while it knows the tray
            # mapping, which the printer sends once at print start: an HA restart
            # mid-print leaves just the total. Keep the split from when it appeared.
            snapshot = self.ledger.snapshot
            if snapshot is not None and not snapshot.get("booked") and (weights := self.printer.print_weights()):
                snapshot["weights"] = {str(flat): grams for flat, grams in weights.items()}
                self.hass.async_create_task(self._save())
            return
        if event.data["entity_id"] == self.printer.progress_entity:
            if self.printer.status() in BUSY_STATES:
                try:
                    self.ledger.progress = int(float(new.state))
                except ValueError:
                    return
                self.hass.async_create_task(self._save())
            return
        self.hass.async_create_task(self.on_status_change(old.state if old else None, new.state))

    async def on_status_change(self, old: str | None, new: str) -> None:
        if new in BUSY_STATES and old in IDLE_STATES:
            assignment = await self._current_assignment()
            self.ledger.snapshot = {"start_ts": time.time(), "assignment": assignment, "runouts": {}, "booked": False}
            # Reprinting the same plate leaves the split unchanged, so no change
            # event follows: take it now; a different print's split replaces it.
            if weights := self.printer.print_weights():
                self.ledger.snapshot["weights"] = {str(flat): grams for flat, grams in weights.items()}
            self.ledger.progress = None
            await self._save()
        elif new == "pause" and (runouts := (self.ledger.snapshot or {}).get("runouts")):
            now = time.time()
            if recent := [k for k, r in runouts.items() if now - r["ts"] < RUNOUT_PAUSE_GRACE_S]:
                for key in recent:
                    del runouts[key]
                await self._save()
        elif (new in END_STATES and self.ledger.snapshot is not None
              and not self.ledger.snapshot.get("booked") and not self._fetching):
            self._fetching = True
            self.entry.async_create_task(self.hass, self._fetch_job_loop(), f"{DOMAIN} job fetch")

    async def _current_assignment(self) -> dict[str, int]:
        try:
            spools = await self.spoolman.get_spools(allow_archived=False)
        except SpoolmanError:
            return {}
        return {slot: s["id"] for s in spools if (slot := extra_get(s, EXTRA_SLOT))}

    async def _fetch_job_loop(self) -> None:
        try:
            for _ in range(JOB_FETCH_ATTEMPTS):
                if await self.process_finished_job():
                    return
                await asyncio.sleep(JOB_FETCH_DELAY_S)
            await self.notify("A print finished but the printer did not report its filament usage; "
                              "it could not be booked.")
        finally:
            self._fetching = False

    def _local_job(self, snapshot: dict) -> dict | None:
        """Synthesize the job shape the ledger consumes from ha-bambulab's local
        per-tray grams. None until the printer reports weights."""
        weights = self.printer.print_weights() or {
            int(flat): grams for flat, grams in (snapshot.get("weights") or {}).items()}
        if not weights:
            return None
        finished = self.printer.status() == "finish"
        return {"id": int(snapshot["start_ts"]),
                "status": TASK_FINISHED if finished else TASK_FAILED,
                "amsDetailMapping": [{"ams": flat, "weight": g} for flat, g in weights.items()]}

    async def process_finished_job(self) -> bool:
        """Book the job of the current snapshot. True when done (booked or already booked)."""
        snapshot = self.ledger.snapshot
        if snapshot is None or snapshot.get("booked"):
            return True
        job = self._local_job(snapshot)
        if job is None:
            return False
        async with self._ledger_lock:
            entries = plan_entries(job, snapshot["assignment"], self.ledger.progress, time.time(),
                                   snapshot.get("runouts"))
            if not self.ledger.add_job(job, entries, time.time()):
                entries = []
            snapshot["booked"] = True
            await self._save()
        await self.apply_pending()
        for entry in entries:
            if entry.state == "skipped":
                await self.notify(f"Print usage {entry.grams} g from tray {entry.ams} not booked: {entry.note}")
        await self._notify_refills(entries)
        return True

    async def _notify_refills(self, entries: list[Entry]) -> None:
        """One message per auto-refill split: the grams are an estimate, so ask for a weigh-in."""
        name = self.printer.name()
        for backup in (e for e in entries if e.share == "backup"):
            first = next(e for e in entries if (e.job_id, e.ams) == (backup.job_id, backup.ams) and not e.share)
            slot = slot_from_flat(backup.ams)
            await self.notify(
                f"Filament ran out on {slot.label(name)} ({first.note}); the printer carried on with spool "
                f"{backup.spool_id}. Split: {first.grams} g to spool {first.spool_id} (now empty) and "
                f"{backup.grams} g to spool {backup.spool_id}. The split is an estimate from the print "
                f"progress: weigh spool {backup.spool_id} to correct it.")

    async def catch_up_jobs(self) -> None:
        """Book a print that ended while HA was down, when its snapshot is still open
        and the printer still reports that print's per-tray grams.

        There is no job history to fall back on, so a print whose weights the
        printer has already overwritten (another print started since) cannot be
        recovered; apply_pending still retries any entries booked but not yet written."""
        snapshot = self.ledger.snapshot
        if (snapshot is not None and not snapshot.get("booked")
                and self.printer.status() in END_STATES and self._local_job(snapshot) is not None):
            await self.process_finished_job()
        await self.apply_pending()

    async def _interval_ledger(self, _now=None) -> None:
        await self.apply_pending()
        self.ledger.prune(time.time())
        await self._save()

    async def apply_pending(self) -> None:
        async with self._ledger_lock:
            for entry in self.ledger.pending():
                try:
                    await apply_entry(entry, self.spoolman, self._save)
                except Exception as err:  # noqa: BLE001 - stays pending, retried later
                    self._error(f"Usage {entry.key}: {err}")
            stuck = [e for e in self.ledger.pending() if time.time() - e.created > STUCK_AFTER_S]
            if stuck and not self._stuck_notified:
                self._stuck_notified = True
                await self.notify(f"{len(stuck)} usage deduction(s) pending for over an hour.")
            await self._save()

    # ------------------------------------------------------------ diagnostics

    def diagnostics(self) -> dict:
        return {
            "state": {k: str(v) for k, v in self.state.items()},
            "ledger": {"pending": len(self.ledger.pending()), "entries": len(self.ledger.entries),
                       "done_jobs": len(self.ledger.done_jobs), "has_snapshot": self.ledger.snapshot is not None},
            "trays": [t.slot.key for t in self.printer.trays()],
            "bambuddy": {
                "configured": self.bambuddy is not None,
                "printer_id": self.bambuddy_printer,
                "slots": None if self._bb_seen is None else {s.key: i for s, i in self._bb_seen.items()},
                "gateway": self.proxy.url if self.proxy is not None else None,
                "usage_ignored": list(self._usage_ignored),
            },
        }
