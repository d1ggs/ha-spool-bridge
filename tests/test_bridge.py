from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spool_bridge.bridge import Bridge
from custom_components.spool_bridge.model import SlotRef
from custom_components.spool_bridge.printer import Printer
from tests.test_printer import SERIAL, setup_printer


def sm_spool(sid, slot=None, archived=False, rfid=None):
    extra = {}
    if slot:
        extra["bambu_slot"] = f'"{slot}"'
    if rfid:
        extra["bambu_rfid"] = f'"{rfid}"'
    return {"id": sid, "archived": archived, "remaining_weight": 400, "initial_weight": 1000,
            "used_weight": 600, "extra": extra,
            "filament": {"name": "PolyTerra PLA", "material": "PLA", "color_hex": "333333", "weight": 1000,
                         "settings_extruder_temp": 215, "vendor": {"name": "Polymaker"}, "extra": {}}}


@pytest.fixture
async def bridge(hass):
    setup_printer(hass)
    entry = MockConfigEntry(domain="spool_bridge", data={
        "spoolman_url": "http://sm", "printer_serial": SERIAL, "notify_service": "mobile_app_test",
        "region": "global", "email": "x@example.com", "token": "t"})
    entry.add_to_hass(hass)
    spools = {42: sm_spool(42), 7: sm_spool(7, slot="ams:1:1")}
    sm = MagicMock()
    sm.get_spool = AsyncMock(side_effect=lambda i: spools.get(i))
    sm.get_spools = AsyncMock(side_effect=lambda allow_archived=True: list(spools.values()))
    sm.set_spool_extra = AsyncMock()
    sm.use_weight = AsyncMock()
    sm.listen = AsyncMock()
    cloud = MagicMock()
    cloud.get_tasks.return_value = []
    cloud.get_spools.return_value = []
    notes, filament_calls = [], []

    async def notify(call):
        notes.append(call.data)

    async def set_filament(call):
        filament_calls.append(call.data)

    hass.services.async_register("notify", "mobile_app_test", notify)
    hass.services.async_register("bambu_lab", "set_filament", set_filament)
    b = Bridge(hass, entry, sm, cloud, Printer(hass, SERIAL))
    b.request_reconcile = AsyncMock()
    b.notes, b.filament_calls, b.spools = notes, filament_calls, spools
    yield b
    await b.async_stop()


def tray_entity(bridge, key):
    return bridge.printer.tray_for(SlotRef.parse(key)).entity_id


async def load(hass, entity_id, empty):
    hass.states.async_set(entity_id, "Generic PLA", {"empty": empty, "tray_uuid": "0" * 32, "type": "PLA"})
    await hass.async_block_till_done()


async def test_scan_then_load_assigns(hass, bridge, freezer):
    await bridge.async_start()
    await bridge.on_tag("spool/show/42")
    ent = tray_entity(bridge, "ams:0:2")
    await load(hass, ent, True)
    await load(hass, ent, False)
    freezer.tick(11)
    await bridge.async_tick()
    bridge.spoolman.set_spool_extra.assert_any_await(42, {"bambu_slot": "ams:0:2"}, location="X2D AMS 1 · slot 3")
    assert bridge.filament_calls[0]["tray_info_idx"] == "GFL99"
    assert bridge.filament_calls[0]["nozzle_temp_min"] == 205
    assert "AMS 1 · slot 3" in bridge.notes[-1]["message"]


async def test_scan_unknown_spool(hass, bridge):
    await bridge.async_start()
    await bridge.on_tag("spool-999")
    assert bridge.window.armed is None
    assert "Unknown spool" in bridge.notes[-1]["message"]


async def test_unload_unassigns(hass, bridge):
    await bridge.async_start()
    ent = tray_entity(bridge, "ams:1:1")
    await load(hass, ent, True)
    bridge.spoolman.set_spool_extra.assert_any_await(7, {"bambu_slot": None}, location=None)


async def test_unavailable_tray_does_not_unassign(hass, bridge):
    await bridge.async_start()
    ent = tray_entity(bridge, "ams:1:1")
    hass.states.async_set(ent, "unavailable", {})
    await hass.async_block_till_done()
    await load(hass, ent, False)
    bridge.spoolman.set_spool_extra.assert_not_awaited()


async def test_finish_twice_books_once(hass, bridge):
    await bridge.async_start()
    status = bridge.printer.status_entity
    hass.states.async_set(status, "idle")
    await hass.async_block_till_done()
    hass.states.async_set(status, "running")
    await hass.async_block_till_done()
    start = bridge.ledger.snapshot["start_ts"]
    from datetime import UTC, datetime
    iso = datetime.fromtimestamp(start, UTC).isoformat().replace("+00:00", "Z")
    bridge.cloud.get_tasks.return_value = [{"id": 5, "deviceId": SERIAL, "status": 2, "startTime": iso,
                                            "endTime": iso, "amsDetailMapping": [{"ams": 5, "weight": 12.0}]}]
    for _ in range(2):
        hass.states.async_set(status, "finish")
        await hass.async_block_till_done()
        await bridge.process_finished_job()
        hass.states.async_set(status, "idle")
        await hass.async_block_till_done()
    bridge.spoolman.use_weight.assert_awaited_once_with(7, 12.0)


async def _start_print(hass, bridge, status):
    hass.states.async_set(status, "idle")
    await hass.async_block_till_done()
    hass.states.async_set(status, "running")
    await hass.async_block_till_done()


def _job_for(bridge, status_code=2, start_offset=0):
    from datetime import UTC, datetime
    start = bridge.ledger.snapshot["start_ts"] + start_offset
    iso = datetime.fromtimestamp(start, UTC).isoformat().replace("+00:00", "Z")
    return {"id": 9, "deviceId": SERIAL, "status": status_code, "startTime": iso, "endTime": iso,
            "amsDetailMapping": [{"ams": 5, "weight": 20.0}]}


async def test_offline_blip_keeps_snapshot_and_books(hass, bridge):
    await bridge.async_start()
    status = bridge.printer.status_entity
    await _start_print(hass, bridge, status)
    first = dict(bridge.ledger.snapshot)
    bridge.ledger.progress = 40
    for state in ("offline", "running"):
        hass.states.async_set(status, state)
        await hass.async_block_till_done()
    assert bridge.ledger.snapshot["start_ts"] == first["start_ts"]
    assert bridge.ledger.progress == 40
    bridge.cloud.get_tasks.return_value = [_job_for(bridge)]
    hass.states.async_set(status, "offline")
    await hass.async_block_till_done()
    hass.states.async_set(status, "finish")  # offline -> finish must still book
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    bridge.spoolman.use_weight.assert_awaited_once_with(7, 20.0)


async def test_catch_up_uses_snapshot(hass, bridge):
    await bridge.async_start()
    await _start_print(hass, bridge, bridge.printer.status_entity)
    bridge.ledger.progress = 50
    bridge.ledger.last_end_ts = bridge.ledger.snapshot["start_ts"] - 10
    bridge.spools[7]["extra"] = {}  # spool 7 unloaded since the snapshot
    bridge.cloud.get_tasks.return_value = [_job_for(bridge, status_code=3)]
    await bridge.catch_up_jobs()
    bridge.spoolman.use_weight.assert_awaited_once_with(7, 10.0)


async def test_external_holder_changes_ignored(hass, bridge):
    await bridge.async_start()
    bridge.spools[42]["extra"] = {"bambu_slot": '"ext:0"'}
    ent = tray_entity(bridge, "ext:0")
    hass.states.async_set(ent, "Bambu TPU", {"empty": False, "tray_uuid": "0" * 32})
    await hass.async_block_till_done()
    hass.states.async_set(ent, "Bambu TPU", {"empty": True, "tray_uuid": "0" * 32})
    await hass.async_block_till_done()
    bridge.spoolman.set_spool_extra.assert_not_awaited()


async def test_tick_errors_do_not_kill_ticker(hass, bridge):
    bridge.async_tick = AsyncMock(side_effect=KeyError("id"))
    await bridge._safe_tick()  # must not raise


async def test_spoolman_event_tolerates_garbage(hass, bridge):
    bridge._spoolman_event(["not", "a", "dict"])
    bridge._spoolman_event({"resource": "spool"})
    await hass.async_block_till_done()


async def test_set_filament_failure_retried_and_reported(hass, bridge):
    await bridge.async_start()
    attempts = []

    async def failing(call):
        attempts.append(call)
        raise RuntimeError("printer busy")

    hass.services.async_register("bambu_lab", "set_filament", failing)
    await bridge.assign(42, SlotRef("ams", 0, 2))
    assert len(attempts) == 3
    assert "profile" in bridge.notes[-1]["message"].lower()
    assert "✓" not in bridge.notes[-1]["message"]
