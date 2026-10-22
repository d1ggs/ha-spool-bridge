"""The bridge keeps Bambuddy (Spoolman mode) and Spoolman in step."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.spool_bridge.bambuddy import BambuddyError
from custom_components.spool_bridge.model import SlotRef
from tests.test_bridge import RFID, bridge, sm_spool, tray, tray_entity  # noqa: F401 - fixture


@pytest.fixture
def bb(bridge):  # noqa: F811
    """Bambuddy client stand-in with a live assignment table, and a Spoolman
    stand-in whose extra/location writes actually land on the spools."""
    table: dict[SlotRef, int] = {}

    async def assign(printer_id, spool_id, slot):
        table[slot] = spool_id

    async def unassign(spool_id):
        for slot in [s for s, sid in table.items() if sid == spool_id]:
            del table[slot]

    client = MagicMock()
    client.assignments = AsyncMock(side_effect=lambda printer_id: dict(table))
    client.assign = AsyncMock(side_effect=assign)
    client.unassign = AsyncMock(side_effect=unassign)
    bridge.bambuddy, bridge.bambuddy_printer = client, 1

    async def set_spool_extra(spool_id, values, location=None):
        spool = bridge.spools[spool_id]
        for key, value in values.items():
            if value is None:
                spool["extra"].pop(key, None)
            else:
                spool["extra"][key] = json.dumps(value)
        return spool

    bridge.spoolman.set_spool_extra = AsyncMock(side_effect=set_spool_extra)
    client.table = table
    return client


async def idle(hass, bridge):
    hass.states.async_set(bridge.printer.status_entity, "idle")
    await hass.async_block_till_done()


def slot_of(bridge, spool_id):
    raw = bridge.spools[spool_id]["extra"].get("bambu_slot")
    return json.loads(raw) if raw else None


async def test_bridge_assignment_is_pushed_with_its_profile(hass, bridge, bb):
    # Scan -> load (or any bridge assignment) shows up on Bambuddy's slot, and the
    # spool carries the profile the bridge set, so Bambuddy's own push matches it.
    await bridge.async_start()
    await bridge.assign(42, SlotRef("ams", 0, 2))
    assert bb.table.get(SlotRef("ams", 0, 2)) == 42
    assert json.loads(bridge.spools[42]["extra"]["bambu_slicer_filament"]) == "GFL99"


async def test_rfid_spool_is_linked_by_tag_not_assigned(hass, bridge, bb):
    # Bambuddy pushes a profile on assignment, which would overwrite the tray's
    # RFID-read settings; RFID spools are linked through Spoolman's tag instead.
    bridge.spools[5] = sm_spool(5, rfid=RFID)
    await tray(hass, tray_entity(bridge, "ams:0:3"), "Bambu PLA Basic", False, uuid=RFID)
    await bridge.sync_rfid_trays()
    assert json.loads(bridge.spools[5]["extra"]["tag"]) == RFID
    assert bb.table == {}


async def test_unload_removes_bambuddy_assignment(hass, bridge, bb):
    await bridge.async_start()
    bb.table[SlotRef("ams", 1, 1)] = 7
    await bridge.unassign(SlotRef("ams", 1, 1))
    assert bb.table == {}


async def test_assignment_made_in_bambuddy_lands_in_spoolman(hass, bridge, bb):
    # e.g. Bambuddy QR label -> Assign to AMS: the bridge records the slot so the
    # print's usage goes to that spool. Bambuddy already set the tray profile.
    await bridge.sync_from_bambuddy()  # first look
    bb.table[SlotRef("ams", 0, 2)] = 42
    await bridge.sync_from_bambuddy()
    assert slot_of(bridge, 42) == "ams:0:2"
    assert bridge.filament_calls == []


async def test_unassignment_made_in_bambuddy_lands_in_spoolman(hass, bridge, bb):
    bb.table[SlotRef("ams", 1, 1)] = 7  # in step with Spoolman
    await bridge.sync_from_bambuddy()
    del bb.table[SlotRef("ams", 1, 1)]
    await bridge.sync_from_bambuddy()
    assert slot_of(bridge, 7) is None


async def test_missing_bambuddy_assignment_is_pushed(hass, bridge, bb):
    # Spool 7 sits at ams:1:1 in Spoolman (assigned before the integration, or a
    # push that failed): the next sync puts it on Bambuddy's slot.
    await idle(hass, bridge)
    await bridge.sync_from_bambuddy()
    assert bb.table == {SlotRef("ams", 1, 1): 7}


async def test_first_sync_conflict_keeps_spoolman(hass, bridge, bb):
    bb.table[SlotRef("ams", 1, 1)] = 42  # Bambuddy disagrees about ams:1:1
    await idle(hass, bridge)
    await bridge.sync_from_bambuddy()
    assert slot_of(bridge, 7) == "ams:1:1" and slot_of(bridge, 42) is None
    assert bb.table == {SlotRef("ams", 1, 1): 7}


async def test_bambuddy_assignment_on_rfid_tray_is_ignored(hass, bridge, bb):
    await tray(hass, tray_entity(bridge, "ams:0:3"), "Bambu PLA Basic", False, uuid=RFID)
    await bridge.sync_from_bambuddy()
    bb.table[SlotRef("ams", 0, 3)] = 42
    await bridge.sync_from_bambuddy()
    assert slot_of(bridge, 42) is None


async def test_bambuddy_down_is_reported_not_raised(hass, bridge, bb):
    bb.assignments.side_effect = BambuddyError("unreachable")
    await bridge.sync_from_bambuddy()
    assert "Bambuddy" in bridge.state["last_error"]


async def test_weights_saved_when_print_starts(hass, bridge):
    # Reprinting the same plate leaves ha-bambulab's split unchanged, so no
    # change event fires: the split must be taken when the print starts.
    from tests.test_bridge import _start_print, set_local_weights
    await bridge.async_start()
    set_local_weights(hass, bridge, {"AMS 2 Tray 2": 12.0})
    await hass.async_block_till_done()
    await _start_print(hass, bridge, bridge.printer.status_entity)
    assert bridge.ledger.snapshot["weights"] == {"5": 12.0}


async def test_ignored_bambuddy_usage_is_recorded(hass, bridge):
    bridge.on_bambuddy_usage(7, 12.5)
    assert bridge.diagnostics()["bambuddy"]["usage_ignored"][-1][1:] == (7, 12.5)


async def test_sync_defers_pushes_for_slots_the_running_print_uses(hass, bridge, bb):
    # Bambuddy's assignment sends the slot's profile to the printer: the initial
    # catch-up must not touch trays a running print is drawing from.
    from tests.test_bridge import _start_print
    bridge.spools[42]["extra"]["bambu_slot"] = '"ams:0:2"'
    await _start_print(hass, bridge, bridge.printer.status_entity)
    bridge.ledger.snapshot = {"start_ts": 1, "assignment": {}, "runouts": {}, "booked": False,
                              "weights": {"5": 12.0}}  # ams:1:1 (spool 7) in use
    await bridge.sync_from_bambuddy()
    assert bb.table == {SlotRef("ams", 0, 2): 42}
    bridge.ledger.snapshot["weights"] = None  # split unknown: every slot counts as in use
    bb.table.clear()
    bridge._bb_seen = None
    await bridge.sync_from_bambuddy()
    assert bb.table == {}
    hass.states.async_set(bridge.printer.status_entity, "finish")
    await hass.async_block_till_done()
    await bridge.sync_from_bambuddy()
    assert bb.table == {SlotRef("ams", 0, 2): 42, SlotRef("ams", 1, 1): 7}
