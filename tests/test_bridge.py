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
async def bridge(hass, monkeypatch):
    monkeypatch.setattr("custom_components.spool_bridge.printer.SET_FILAMENT_CONFIRM_S", 0)
    monkeypatch.setattr("custom_components.spool_bridge.bridge.JOB_FETCH_DELAY_S", 0)
    monkeypatch.setattr("custom_components.spool_bridge.bridge.JOB_FETCH_ATTEMPTS", 2)
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
        old = hass.states.get(call.data["entity_id"])  # the printer echoes the profile it took
        hass.states.async_set(call.data["entity_id"], "Generic PLA",
                              {**(old.attributes if old else {}), "filament_id": call.data["tray_info_idx"]})

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


def set_local_weights(hass, bridge, mapping):
    """ha-bambulab's print_weight sensor: {"AMS u Tray t": grams, ...}."""
    hass.states.async_set("sensor.print_weight", str(sum(mapping.values())), dict(mapping))


async def test_finish_books_from_local_weights(hass, bridge):
    # Usage now comes from ha-bambulab's per-tray grams, not the Bambu cloud job
    # history. Spool 7 sits at ams:1:1 = "AMS 2 Tray 2".
    await bridge.async_start()
    status = bridge.printer.status_entity
    await _start_print(hass, bridge, status)
    set_local_weights(hass, bridge, {"AMS 2 Tray 2": 12.0})
    hass.states.async_set(status, "finish")
    await hass.async_block_till_done()
    await bridge.process_finished_job()
    bridge.spoolman.use_weight.assert_awaited_once_with(7, 12.0)


async def test_finish_twice_books_once(hass, bridge):
    await bridge.async_start()
    status = bridge.printer.status_entity
    hass.states.async_set(status, "idle")
    await hass.async_block_till_done()
    hass.states.async_set(status, "running")
    await hass.async_block_till_done()
    set_local_weights(hass, bridge, {"AMS 2 Tray 2": 12.0})
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
    set_local_weights(hass, bridge, {"AMS 2 Tray 2": 20.0})
    hass.states.async_set(status, "offline")
    await hass.async_block_till_done()
    hass.states.async_set(status, "finish")  # offline -> finish must still book
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    bridge.spoolman.use_weight.assert_awaited_once_with(7, 20.0)


async def test_catch_up_books_open_snapshot_from_local_weights(hass, bridge):
    # Print ended (failed, 50%) while HA was down: on restart the snapshot is open
    # and the printer still reports the per-tray grams, so catch-up books it.
    await bridge.async_start()
    await _start_print(hass, bridge, bridge.printer.status_entity)
    bridge.ledger.progress = 50
    bridge.spools[7]["extra"] = {}  # spool 7 unloaded since the snapshot
    set_local_weights(hass, bridge, {"AMS 2 Tray 2": 20.0})
    bridge._fetching = True  # suppress the live end-transition loop; exercise catch_up directly
    hass.states.async_set(bridge.printer.status_entity, "failed")
    await hass.async_block_till_done()
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


async def test_assign_skips_set_filament_when_control_blocked(hass, bridge):
    # Cloud-mode firmware blocks set_filament; the cloud slot binding carries the
    # profile instead, so the bridge must not try, and must still report success.
    await bridge.async_start()
    hass.states.async_set("binary_sensor.developer_lan_mode", "off")
    await bridge.assign(42, SlotRef("ams", 0, 2))
    msg = bridge.notes[-1]["message"]
    assert bridge.filament_calls == []
    assert "✓" in msg
    assert bridge.state["last_error"] is None
    # The on-device tray can't be set in cloud mode, so screen-started prints
    # need a manual pick; the notification names exactly what to select.
    assert "screen" in msg.lower()
    assert "PolyTerra PLA" in msg  # the Spoolman filament name
    assert "GFL99" in msg  # the Bambu profile id to pick on the printer


async def test_set_filament_ignored_by_printer_is_reported(hass, bridge):
    # ha-bambulab returns quietly when the printer blocks control commands
    # (seen live 2026-10-22: "requires mqtt encryption", then a timeout), so
    # the tray keeps its old profile and only reading it back shows that.
    await bridge.async_start()
    ent = tray_entity(bridge, "ams:0:2")
    await load(hass, ent, False)
    hass.services.async_register("bambu_lab", "set_filament", lambda call: None)
    await bridge.assign(42, SlotRef("ams", 0, 2))
    assert "profile could not be set" in bridge.notes[-1]["message"]
    assert "✓" not in bridge.notes[-1]["message"]


DB_FILAMENTS = [
    {"id": 12, "name": "Panchroma™ Matte (Formerly PolyTerra™) Earth Brown", "external_id": "polymaker_pla_x"},
    {"id": 3, "name": "PLA Basic™", "external_id": None},  # bridge-made: matched by name, never renamed
    {"id": 14, "name": "PLA-Basic Silver", "external_id": "esun_pla_x"},
]


def with_filaments(bridge):
    bridge.spoolman.get_filaments = AsyncMock(return_value=DB_FILAMENTS)
    bridge.spoolman.patch_filament = AsyncMock()


async def test_rename_filaments_only_touches_database_imports(hass, bridge):
    with_filaments(bridge)
    hass.config_entries.async_update_entry(
        bridge.entry, options={"filament_name_rules": "™\n\\s*\\(Formerly[^)]*\\)"})
    await bridge.rename_filaments()
    bridge.spoolman.patch_filament.assert_awaited_once_with(12, {"name": "Panchroma Matte Earth Brown"})


async def test_rename_filaments_off_without_rules(hass, bridge):
    with_filaments(bridge)
    await bridge.rename_filaments()
    bridge.spoolman.get_filaments.assert_not_awaited()


async def test_reconcile_renames_before_cloud_sync(hass, bridge):
    with_filaments(bridge)
    hass.config_entries.async_update_entry(bridge.entry, options={"filament_name_rules": "™"})
    order = []
    bridge.spoolman.patch_filament.side_effect = lambda *a: order.append("rename")
    bridge.sync.reconcile = AsyncMock(side_effect=lambda: order.append("sync") or [])
    await bridge._reconcile()
    assert order == ["rename", "sync"]


# --- AMS auto-refill: a tray runs dry mid-print and the printer continues from a same-filament tray

async def _runout_print(hass, bridge, extra_spools=()):
    """Print started with spool 7 in ams:1:1 and spool 42 (same filament) in ams:1:2."""
    bridge.spools[42]["extra"] = {"bambu_slot": '"ams:1:2"'}
    for sid, slot in extra_spools:
        bridge.spools[sid] = sm_spool(sid, slot=slot)
    await bridge.async_start()
    await _start_print(hass, bridge, bridge.printer.status_entity)
    bridge.ledger.progress = 60


async def _set_tray(hass, bridge, key, empty, active=False):
    hass.states.async_set(tray_entity(bridge, key), "Generic PLA", {
        "empty": empty, "active": active, "tray_uuid": "0" * 32, "type": "PLA"})
    await hass.async_block_till_done()


async def test_runout_recorded_with_matching_backup(hass, bridge):
    await _runout_print(hass, bridge)
    await _set_tray(hass, bridge, "ams:1:1", empty=True)
    run = bridge.ledger.snapshot["runouts"]["ams:1:1"]
    assert (run["progress"], run["backup"], run["candidates"]) == (60, "ams:1:2", ["ams:1:2"])


async def test_runout_backup_follows_active_tray(hass, bridge):
    await _runout_print(hass, bridge, extra_spools=[(43, "ams:1:3")])
    await _set_tray(hass, bridge, "ams:1:1", empty=True)
    assert bridge.ledger.snapshot["runouts"]["ams:1:1"]["backup"] == "ams:1:2"
    await _set_tray(hass, bridge, "ams:1:3", empty=False, active=True)
    assert bridge.ledger.snapshot["runouts"]["ams:1:1"]["backup"] == "ams:1:3"
    await _set_tray(hass, bridge, "ams:1:0", empty=False, active=True)  # unrelated colour change
    assert bridge.ledger.snapshot["runouts"]["ams:1:1"]["backup"] == "ams:1:3"


async def test_runout_without_same_filament_is_not_recorded(hass, bridge):
    bridge.spools[7]["filament"] = {**bridge.spools[7]["filament"], "color_hex": "FF0000"}
    await _runout_print(hass, bridge)
    await _set_tray(hass, bridge, "ams:1:1", empty=True)
    assert bridge.ledger.snapshot["runouts"] == {}


async def test_no_runout_when_idle_paused_or_unassigned(hass, bridge):
    await _runout_print(hass, bridge)
    await _set_tray(hass, bridge, "ams:1:0", empty=True)  # not in the print-start assignment
    assert bridge.ledger.snapshot["runouts"] == {}
    status = bridge.printer.status_entity
    hass.states.async_set(status, "idle")
    await hass.async_block_till_done()
    await _set_tray(hass, bridge, "ams:1:1", empty=True)
    assert bridge.ledger.snapshot["runouts"] == {}


async def test_pause_right_after_runout_discards_it(hass, bridge):
    await _runout_print(hass, bridge)
    await _set_tray(hass, bridge, "ams:1:1", empty=True)
    hass.states.async_set(bridge.printer.status_entity, "pause")  # a real runout pause: no auto-refill happened
    await hass.async_block_till_done()
    assert bridge.ledger.snapshot["runouts"] == {}


async def test_runout_job_is_split_and_reported(hass, bridge):
    await _runout_print(hass, bridge)
    bridge.spoolman.patch_spool = AsyncMock()
    await _set_tray(hass, bridge, "ams:1:1", empty=True)
    set_local_weights(hass, bridge, {"AMS 2 Tray 2": 20.0})
    hass.states.async_set(bridge.printer.status_entity, "finish")
    await hass.async_block_till_done()
    await bridge.process_finished_job()
    assert [c.args for c in bridge.spoolman.use_weight.await_args_list] == [(7, 12.0), (42, 8.0)]
    bridge.spoolman.patch_spool.assert_awaited_once_with(7, {"used_weight": 1000.0})
    notes = [n["message"] for n in bridge.notes if "estimate" in n["message"]]
    assert len(notes) == 1 and "12" in notes[0] and "8" in notes[0] and "weigh" in notes[0].lower()
    await bridge.process_finished_job()  # already booked: nothing more
    assert bridge.spoolman.use_weight.await_count == 2


async def test_chained_runout_keeps_first_backup(hass, bridge):
    await _runout_print(hass, bridge, extra_spools=[(43, "ams:1:3")])
    await _set_tray(hass, bridge, "ams:1:1", empty=True)  # backup guessed: ams:1:2
    await _set_tray(hass, bridge, "ams:1:2", empty=True)  # it runs dry too
    await _set_tray(hass, bridge, "ams:1:3", empty=False, active=True)
    assert bridge.ledger.snapshot["runouts"]["ams:1:1"]["backup"] == "ams:1:2"
