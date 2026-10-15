import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from custom_components.spool_bridge.usage import (
    Entry, Ledger, apply_entry, parse_ts, plan_entries, select_job,
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
    ledger.snapshot = {"start_ts": 5.0, "assignment": {"ams:1:1": 42}}
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
