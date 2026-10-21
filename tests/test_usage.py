import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from custom_components.spool_bridge.usage import (
    Entry, Ledger, apply_entry, parse_ts, plan_entries, runout_candidates, select_job,
)

TASKS = json.loads((Path(__file__).parent / "fixtures" / "cloud_tasks.json").read_text())["hits"]
DEV = TASKS[0]["deviceId"]


def job(status=2, mapping=None, jid=1, start="2026-10-15T18:14:07Z", end="2026-10-15T18:52:13Z"):
    return {"id": jid, "deviceId": DEV, "status": status, "startTime": start, "endTime": end,
            "amsDetailMapping": mapping if mapping is not None else
            [{"ams": 5, "weight": 10.46}, {"ams": 7, "weight": 0.19}]}


def test_parse_ts():
    assert parse_ts("1970-01-01T00:01:00Z") == 60
    assert parse_ts("bad") is None


def test_select_job_matches_start_time():
    t = select_job(TASKS, DEV, parse_ts(TASKS[0]["startTime"]) + 30)
    assert t["id"] == TASKS[0]["id"]
    assert select_job(TASKS, DEV, parse_ts(TASKS[0]["startTime"]) + 3600) is None
    assert select_job(TASKS, "OTHER", None) is None


def test_plan_finished_job():
    entries = plan_entries(job(), {"ams:1:1": 42, "ams:1:3": 7}, None, now=0)
    assert [(e.ams, e.spool_id, e.grams, e.state) for e in entries] == [
        (5, 42, 10.46, "pending"), (7, 7, 0.19, "pending")]


def test_plan_cancelled_job_uses_progress():
    entries = plan_entries(job(status=3), {"ams:1:1": 42, "ams:1:3": 7}, 50, now=0)
    assert [e.grams for e in entries] == pytest.approx([5.23, 0.1], abs=0.011)


def test_plan_cancelled_unknown_progress_skips():
    entries = plan_entries(job(status=3), {"ams:1:1": 42}, None, now=0)
    assert {e.state for e in entries} == {"skipped"}


def test_plan_unassigned_slot_skipped():
    entries = plan_entries(job(), {"ams:1:1": 42}, None, now=0)
    assert entries[1].state == "skipped" and entries[1].spool_id is None


def test_add_job_is_idempotent():
    ledger = Ledger()
    j = job()
    assert ledger.add_job(j, plan_entries(j, {"ams:1:1": 42}, None, 0), now=0)
    assert not ledger.add_job(j, plan_entries(j, {"ams:1:1": 42}, None, 0), now=1)
    assert len(ledger.entries) == 2
    assert ledger.last_end_ts == parse_ts(j["endTime"])


def test_ledger_roundtrip_and_prune():
    ledger = Ledger()
    j = job()
    ledger.add_job(j, plan_entries(j, {"ams:1:1": 42}, None, 0), now=0)
    ledger.snapshot = {"start_ts": 5.0, "assignment": {"ams:1:1": 42},
                       "runouts": {"ams:1:1": {"progress": 40, "backup": "ams:1:2", "candidates": ["ams:1:2"], "ts": 1.0}}}
    ledger.progress = 12
    again = Ledger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert again.to_dict() == ledger.to_dict()
    again.prune(now=91 * 86400)
    assert list(again.entries) == ["1:5"]  # pending entries are never pruned
    assert again.done_jobs == {}


async def test_apply_entry_normal_and_uncertain():
    save = AsyncMock()
    sm = AsyncMock()
    sm.get_spool.return_value = {"id": 42, "used_weight": 100.0}
    e = Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending")
    await apply_entry(e, sm, save)
    sm.use_weight.assert_awaited_once_with(42, 10.0)
    assert e.state == "applied" and e.used_before == 100.0
    # uncertain retry: the first write already landed
    e2 = Entry(job_id=2, ams=5, spool_id=42, grams=10.0, state="pending", used_before=100.0)
    sm.use_weight.reset_mock()
    sm.get_spool.return_value = {"id": 42, "used_weight": 110.2}
    await apply_entry(e2, sm, save)
    sm.use_weight.assert_not_awaited()
    assert e2.state == "applied"


async def test_apply_entry_write_failure_stays_pending():
    sm = AsyncMock()
    sm.get_spool.return_value = {"id": 42, "used_weight": 100.0}
    sm.use_weight.side_effect = RuntimeError("timeout")
    e = Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending")
    try:
        await apply_entry(e, sm, AsyncMock())
    except RuntimeError:
        pass
    assert e.state == "pending" and e.used_before == 100.0


async def test_apply_entry_deleted_spool_skipped():
    sm = AsyncMock()
    sm.get_spool.return_value = None
    e = Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending")
    await apply_entry(e, sm, AsyncMock())
    assert e.state == "skipped"


RUNOUT = {"ams:1:1": {"progress": 60, "backup": "ams:1:2", "candidates": ["ams:1:2"], "ts": 0.0}}
ASSIGNED = {"ams:1:1": 42, "ams:1:2": 43}


def test_plan_runout_splits_by_progress():
    entries = plan_entries(job(mapping=[{"ams": 5, "weight": 100.0}]), ASSIGNED, None, 0, RUNOUT)
    assert [(e.key, e.spool_id, e.grams, e.state, e.zero) for e in entries] == [
        ("1:5", 42, 60.0, "pending", True), ("1:5:backup", 43, 40.0, "pending", False)]


def test_plan_runout_without_usable_backup_is_unchanged():
    one = [{"ams": 5, "weight": 100.0}]
    for runouts, assignment in (
        ({"ams:1:1": {"progress": 60, "backup": None}}, ASSIGNED),
        (RUNOUT, {"ams:1:1": 42}),  # backup slot no longer has a spool
        (RUNOUT, {"ams:1:1": 42, "ams:1:2": 42}),  # same spool
        ({"ams:1:1": {"progress": None, "backup": "ams:1:2"}}, ASSIGNED),
    ):
        entries = plan_entries(job(mapping=one), assignment, None, 0, runouts)
        assert [(e.key, e.spool_id, e.grams, e.zero) for e in entries] == [("1:5", 42, 100.0, False)]


def test_plan_runout_on_cancelled_job():
    one = [{"ams": 5, "weight": 100.0}]
    entries = plan_entries(job(status=3, mapping=one), ASSIGNED, 80, 0, RUNOUT)
    assert [(e.spool_id, e.grams) for e in entries] == [(42, 60.0), (43, 20.0)]
    entries = plan_entries(job(status=3, mapping=one), ASSIGNED, 50, 0, RUNOUT)  # cancelled before the runout
    assert [(e.spool_id, e.grams) for e in entries] == [(42, 50.0)]


def test_plan_runout_only_affects_its_slot():
    entries = plan_entries(job(), {**ASSIGNED, "ams:1:3": 7}, None, 0, RUNOUT)
    assert [e.key for e in entries] == ["1:5", "1:5:backup", "1:7"]


def test_runout_entries_roundtrip():
    entry = plan_entries(job(), ASSIGNED, None, 0, RUNOUT)[1]
    assert Entry.from_dict(entry.to_dict()) == entry
    old = {"job_id": 1, "ams": 5, "spool_id": 4, "grams": 1.0, "state": "applied"}  # stored before this feature
    assert Entry.from_dict(old).key == "1:5"


def fil_spool(material="PETG", color="111111"):
    return {"filament": {"material": material, "color_hex": color}}


def test_runout_candidates_match_material_and_colour():
    spools = {5: fil_spool(), 11: fil_spool(color="111111"), 12: fil_spool(color="FFFFFF"),
              13: fil_spool(material="PLA"), 14: fil_spool(material="petg", color="#111111")}
    assignment = {"ams:0:2": 5, "ams:0:3": 11, "ams:0:0": 12, "ams:1:0": 13, "ams:1:1": 14, "ams:1:2": 11}
    loaded = {"ams:0:3", "ams:0:0", "ams:1:0", "ams:1:1"}
    assert runout_candidates("ams:0:2", assignment, spools, loaded) == ["ams:0:3", "ams:1:1"]
    assert runout_candidates("ams:0:9", assignment, spools, loaded) == []
    assert runout_candidates("ams:0:2", {"ams:0:2": 5}, spools, loaded) == []


async def test_apply_entry_zero_empties_spool():
    sm = AsyncMock()
    sm.get_spool.return_value = {"id": 42, "used_weight": 100.0, "initial_weight": 1000.0}
    e = Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending", zero=True)
    await apply_entry(e, sm, AsyncMock())
    sm.use_weight.assert_awaited_once_with(42, 10.0)
    sm.patch_spool.assert_awaited_once_with(42, {"used_weight": 1000.0})
    assert e.state == "applied"


async def test_apply_entry_zero_retry_after_patch_failure_does_not_double_book():
    sm = AsyncMock()
    sm.get_spool.return_value = {"id": 42, "used_weight": 100.0, "initial_weight": 1000.0}
    sm.patch_spool.side_effect = RuntimeError("timeout")
    e = Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending", zero=True)
    with pytest.raises(RuntimeError):
        await apply_entry(e, sm, AsyncMock())
    assert e.state == "pending"
    sm.get_spool.return_value = {"id": 42, "used_weight": 110.0, "initial_weight": 1000.0}
    sm.patch_spool.side_effect = None
    await apply_entry(e, sm, AsyncMock())
    sm.use_weight.assert_awaited_once()
    sm.patch_spool.assert_awaited_with(42, {"used_weight": 1000.0})
    assert e.state == "applied"


async def test_apply_entry_zero_leaves_overdrawn_or_unsized_spool_alone():
    sm = AsyncMock()
    for spool in ({"id": 42, "used_weight": 995.0, "initial_weight": 1000.0}, {"id": 42, "used_weight": 5.0}):
        sm.get_spool.return_value = spool
        await apply_entry(Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending", zero=True), sm, AsyncMock())
    sm.patch_spool.assert_not_awaited()


async def test_apply_entry_zero_falls_back_to_filament_weight():
    sm = AsyncMock()
    sm.get_spool.return_value = {"id": 42, "used_weight": 5.0, "filament": {"weight": 750.0}}
    await apply_entry(Entry(job_id=1, ams=5, spool_id=42, grams=10.0, state="pending", zero=True), sm, AsyncMock())
    sm.patch_spool.assert_awaited_once_with(42, {"used_weight": 750.0})
