import os

import aiohttp
import pytest

from custom_components.spool_bridge.spoolman import SpoolmanClient

URL = os.environ.get("SPOOLMAN_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="SPOOLMAN_URL not set")]


@pytest.fixture
async def client(socket_enabled):
    async with aiohttp.ClientSession() as session:
        c = SpoolmanClient(session, URL)
        await c.ensure_extra_fields()
        yield c


async def test_roundtrip(client):
    vendor = await client.ensure_vendor("ZZ Test Vendor")
    assert await client.ensure_vendor("ZZ Test Vendor") == vendor
    fil = await client.ensure_filament(
        vendor, name="Test PLA", material="PLA", color_hex="AF7933", weight=1000,
        spool_weight=200, bambu_filament_id="GFL99",
    )
    assert await client.ensure_filament(
        vendor, name="Test PLA", material="PLA", color_hex="AF7933", weight=1000,
        spool_weight=200, bambu_filament_id="GFL99",
    ) == fil
    spool = await client.create_spool(
        fil, initial_weight=1000, remaining_weight=500,
        extra={"bambu_cloud_id": "123"}, location=None,
    )
    sid = spool["id"]
    updated = await client.set_spool_extra(sid, {"bambu_slot": "ams:1:0"}, location="X2D AMS 2 · slot 1")
    assert updated["extra"]["bambu_slot"] == '"ams:1:0"'
    assert updated["extra"]["bambu_cloud_id"] == '"123"'  # other keys kept
    assert updated["location"] == "X2D AMS 2 · slot 1"
    cleared = await client.set_spool_extra(sid, {"bambu_slot": None}, location=None)
    assert "bambu_slot" not in cleared["extra"]
    used = await client.use_weight(sid, 12.5)
    assert used["remaining_weight"] == pytest.approx(487.5)
    assert (await client.get_spool(sid))["id"] == sid
    assert await client.get_spool(999999) is None
    assert any(s["id"] == sid for s in await client.get_spools())
