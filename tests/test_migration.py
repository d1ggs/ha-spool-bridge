import json
from pathlib import Path
from unittest.mock import AsyncMock

from custom_components.spool_bridge.migration import describe, import_spools, plan_migration

CLOUD = json.loads((Path(__file__).parent / "fixtures" / "cloud_spools.json").read_text())["hits"]


def test_plan_split():
    imports, deletes = plan_migration(CLOUD)
    assert {c["createType"] for c in imports} == {"ams"}
    assert {c["createType"] for c in deletes} == {"manual"}
    assert len(imports) + len(deletes) == len(CLOUD)
    text = describe(imports, deletes)
    assert f"Import {len(imports)}" in text and f"Delete {len(deletes)}" in text


async def test_import_creates_and_skips_linked():
    imports, _ = plan_migration(CLOUD)
    mounted = next(c for c in imports if c.get("inPrinter"))
    sm = AsyncMock()
    already = next(c for c in imports if not c.get("inPrinter"))
    sm.get_spools.return_value = [{"id": 1, "extra": {"bambu_cloud_id": f'"{already["id"]}"'}}]
    sm.ensure_vendor.return_value = 5
    sm.ensure_filament.return_value = 9
    created = await import_spools(sm, imports, mounted["devId"], "X2D")
    assert created == len(imports) - 1
    kwargs = [c.kwargs for c in sm.create_spool.await_args_list]
    m = next(k for k in kwargs if k["extra"]["bambu_cloud_id"] == str(mounted["id"]))
    assert m["extra"]["bambu_slot"] == f"ams:{mounted['amsId']}:{mounted['slotId']}"
    assert m["extra"]["bambu_rfid"] == mounted["RFID"]
    assert m["location"].startswith("X2D AMS")


def test_only_manual_spools_are_deleted():
    odd = [{"id": 1, "createType": "ams", "RFID": ""}, {"id": 2, "createType": "other"},
           {"id": 3, "createType": "manual"}]
    imports, deletes = plan_migration(odd)
    assert imports == [] and [c["id"] for c in deletes] == [3]
