import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.spool_bridge.bambuddy import BambuddyClient, BambuddyError
from custom_components.spool_bridge.model import SlotRef

BASE = "http://bambuddy.local:8000"
SERIAL = "FAKEPRINTER0001"


def client(hass):
    return BambuddyClient(async_get_clientsession(hass), BASE + "/", "secret-key")


async def test_printer_id_by_serial_sends_api_key(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/v1/printers/", json=[
        {"id": 3, "serial_number": "OTHER"}, {"id": 1, "serial_number": SERIAL}])
    assert await client(hass).printer_id(SERIAL) == 1
    assert aioclient_mock.mock_calls[0][3]["X-API-Key"] == "secret-key"


async def test_printer_id_unknown_serial(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/v1/printers/", json=[{"id": 3, "serial_number": "OTHER"}])
    with pytest.raises(BambuddyError, match="not set up in Bambuddy"):
        await client(hass).printer_id(SERIAL)


async def test_auth_failure_is_reported(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/v1/printers/", status=401, text="bad key")
    with pytest.raises(BambuddyError, match="401"):
        await client(hass).printer_id(SERIAL)


async def test_assignments_mapped_to_slots(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/v1/spoolman/inventory/slot-assignments/all?printer_id=1", json=[
        {"printer_id": 1, "ams_id": 0, "tray_id": 2, "spoolman_spool_id": 12},
        {"printer_id": 1, "ams_id": 255, "tray_id": 1, "spoolman_spool_id": 5},
        {"printer_id": 1, "ams_id": 128, "tray_id": 0, "spoolman_spool_id": 9},  # AMS HT: out of scope
    ])
    assert await client(hass).assignments(1) == {SlotRef("ams", 0, 2): 12, SlotRef("ext", 1): 5}


async def test_assign_and_unassign(hass, aioclient_mock):
    aioclient_mock.post(f"{BASE}/api/v1/spoolman/inventory/slot-assignments", json={})
    aioclient_mock.delete(f"{BASE}/api/v1/spoolman/inventory/slot-assignments/12", json={})
    aioclient_mock.delete(f"{BASE}/api/v1/spoolman/inventory/slot-assignments/13", status=404)
    c = client(hass)
    await c.assign(1, 12, SlotRef("ams", 1, 3))
    await c.unassign(12)
    await c.unassign(13)  # already gone: not an error
    body = aioclient_mock.mock_calls[0][2]
    assert body == {"spoolman_spool_id": 12, "printer_id": 1, "ams_id": 1, "tray_id": 3}
