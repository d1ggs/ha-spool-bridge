from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.spool_bridge.cloud_sync import (
    Bind, CloudSync, Create, Delete, Link, Unbind, Update, plan,
)
from custom_components.spool_bridge.model import SlotRef
from custom_components.spool_bridge.printer import TrayInfo

DEV = "FAKEPRINTER0001"


def spool(sid, *, cloud=None, rfid=None, slot=None, archived=False, remaining=500):
    extra = {}
    if cloud:
        extra["bambu_cloud_id"] = f'"{cloud}"'
    if rfid:
        extra["bambu_rfid"] = f'"{rfid}"'
    if slot:
        extra["bambu_slot"] = f'"{slot}"'
    return {"id": sid, "archived": archived, "remaining_weight": remaining, "initial_weight": 1000,
            "extra": extra, "filament": {"name": "PLA Matte", "material": "PLA", "color_hex": "FFFFFF",
                                          "weight": 1000, "vendor": {"name": "Bambu Lab"},
                                          "extra": {"bambu_filament_id": '"GFA01"'}}}


def cloud(cid, *, rfid="", net=500, in_printer=False, ams=None, slot=""):
    return {"id": cid, "RFID": rfid, "filamentVendor": "Bambu Lab", "filamentType": "PLA",
            "filamentName": "PLA Matte", "filamentId": "GFA01", "color": "#FFFFFFFF", "netWeight": net,
            "totalNetWeight": 1000, "inPrinter": in_printer, "devId": DEV if in_printer else "",
            "amsId": ams, "slotId": slot}


def test_in_sync_has_no_actions():
    assert plan([spool(1, cloud=10)], [cloud(10)], DEV) == []


def test_create_update_delete():
    actions = plan([spool(1), spool(2, cloud=20, remaining=300), spool(3, cloud=30, archived=True)],
                   [cloud(20), cloud(30)], DEV)
    assert Create(1, actions[0].payload) == actions[0]
    assert Update(2, 20, {"netWeight": 300}) in actions
    assert Delete(3, 30) in actions


def test_link_by_rfid_and_skip_unregistered_rfid():
    actions = plan([spool(1, rfid="AB" * 16), spool(2, rfid="CD" * 16)], [cloud(10, rfid="AB" * 16)], DEV)
    assert actions == [Link(1, 10)]


def test_missing_cloud_spool_is_recreated():
    actions = plan([spool(1, cloud=99)], [], DEV)
    assert isinstance(actions[0], Create)


def test_bind_and_unbind():
    actions = plan([spool(1, cloud=10, slot="ams:1:0"), spool(2, cloud=20)],
                   [cloud(10), cloud(20, in_printer=True, ams=0, slot="3")], DEV)
    assert Bind(10, SlotRef("ams", 1, 0), "") in actions
    assert Unbind(SlotRef("ams", 0, 3)) in actions


def test_no_unbind_when_slot_rebound():
    actions = plan([spool(1, cloud=10, slot="ams:0:3"), spool(2, cloud=20)],
                   [cloud(10), cloud(20, in_printer=True, ams=0, slot="3")], DEV)
    assert Unbind(SlotRef("ams", 0, 3)) not in actions


def test_no_unbind_for_rfid_spool():
    # The printer owns RFID spool positions: the cloud rejects manual unbinds
    # (code 4, even on an empty slot) and clears them once another spool loads.
    actions = plan([spool(1, cloud=10, rfid="AB" * 16)],
                   [cloud(10, rfid="AB" * 16, in_printer=True, ams=0, slot="1")], DEV)
    assert actions == []


def test_external_slot_not_bound():
    assert plan([spool(1, cloud=10, slot="ext:0")], [cloud(10)], DEV) == []


def test_other_printer_binding_ignored():
    c = cloud(20, in_printer=True, ams=0, slot="1")
    c["devId"] = "OTHERPRINTER"
    assert plan([spool(2, cloud=20)], [c], DEV) == []


def _sync(cloud_spools, spools):
    hass = MagicMock()

    async def run(func, *args):
        return func(*args)

    hass.async_add_executor_job = run
    client = MagicMock()
    client.get_spools.return_value = cloud_spools
    sm = MagicMock()
    sm.get_spools = AsyncMock(return_value=spools)
    sm.set_spool_extra = AsyncMock()
    printer = MagicMock()
    printer.tray_for.return_value = TrayInfo("sensor.t", SlotRef("ams", 1, 0), "FAKEAMS000000002", 3)
    return CloudSync(hass, client, sm, printer, DEV), client, sm


async def test_bind_failure_is_reported():
    sync, client, _ = _sync([cloud(10)], [spool(1, cloud=10, slot="ams:1:0")])
    client.slot_mappings_sync.return_value = [{"code": 4}]
    await sync.reconcile()
    assert sync.errors and "code 4" in sync.errors[0]
    client.slot_mappings_sync.assert_called_once_with(DEV, [{
        "amsId": 1, "amsSn": "FAKEAMS000000002", "amsType": 3, "rfid": "", "slotId": "0", "spoolId": 10}])


async def test_create_links_new_cloud_id():
    sync, client, sm = _sync([], [spool(1)])
    new = cloud(77)
    client.get_spools.side_effect = [[], [new]]
    await sync.reconcile()
    client.create_spool.assert_called_once()
    sm.set_spool_extra.assert_awaited_once_with(1, {"bambu_cloud_id": "77"})


def test_unlinked_matching_cloud_entry_is_adopted():
    orphan = cloud(55)
    orphan["createType"] = "manual"
    orphan["filamentVendor"], orphan["color"] = "Bambu Lab", "#FFFFFF"
    actions = plan([spool(1), spool(2)], [orphan], DEV)
    assert Link(1, 55) in actions
    assert any(isinstance(a, Create) and a.spool_id == 2 for a in actions)


async def test_create_sends_fields_the_cloud_requires():
    sync, client, _ = _sync([], [spool(1)])
    client.get_spools.side_effect = [[], [cloud(77)]]
    await sync.reconcile()
    body = client.create_spool.call_args.args[0]
    assert body["createType"] == "manual"
    assert body["colorType"] == 2 and body["isSupport"] is False
    assert body["filamentName"] == "PLA Matte"
