"""Per-job filament usage: plan deductions from cloud jobs, apply them exactly once."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .const import JOB_MATCH_TOLERANCE_S, LEDGER_TTL_DAYS, TASK_FAILED, TASK_FINISHED, USED_WEIGHT_TOLERANCE_G
from .model import hex6, slot_from_flat


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Entry:
    job_id: int
    ams: int
    spool_id: int | None
    grams: float
    state: str  # pending | applied | skipped
    used_before: float | None = None
    note: str = ""
    created: float = 0.0
    share: str = ""  # "backup": the part of a runout slot's grams drawn from the auto-refill spool
    zero: bool = False  # the spool ran out for real: leave it empty after booking

    @property
    def key(self) -> str:
        return f"{self.job_id}:{self.ams}" + (f":{self.share}" if self.share else "")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Entry:
        return cls(**data)


def select_job(tasks: list[dict], dev_id: str, start_ts: float | None) -> dict | None:
    """The finished/failed job of this printer that started at `start_ts` (newest if None)."""
    jobs = [t for t in tasks if t.get("deviceId") == dev_id and t.get("status") in (TASK_FINISHED, TASK_FAILED)]
    if start_ts is None:
        return jobs[0] if jobs else None
    scored = [(abs((parse_ts(t.get("startTime")) or 0) - start_ts), t) for t in jobs]
    scored = [s for s in scored if s[0] <= JOB_MATCH_TOLERANCE_S]
    return min(scored, key=lambda s: s[0])[1] if scored else None


def _filament_key(spool: dict) -> tuple[str, str]:
    fil = spool.get("filament") or {}
    return str(fil.get("material") or "").upper(), hex6(fil.get("color_hex"))


def runout_candidates(slot_key: str, assignment: dict[str, int], spools: dict[int, dict], loaded: set[str]) -> list[str]:
    """Loaded slots holding the same material + colour as the spool that ran out: possible auto-refill feeds."""
    own = spools.get(assignment.get(slot_key))
    if own is None:
        return []
    want = _filament_key(own)
    return sorted(k for k, sid in assignment.items()
                  if k != slot_key and k in loaded and sid in spools and _filament_key(spools[sid]) == want)


def plan_entries(job: dict, assignment: dict[str, int], progress: int | None, now: float,
                 runouts: dict[str, dict] | None = None) -> list[Entry]:
    """`runouts` (slot key -> {progress, backup}) splits a slot's grams between the spool that ran out
    (up to the print progress at the runout) and its auto-refill backup (the rest)."""
    if job.get("status") == TASK_FINISHED:
        factor: float | None = 1.0
    else:
        factor = None if progress is None else max(0, min(100, progress)) / 100
    entries = []
    for item in job.get("amsDetailMapping") or []:
        if not isinstance(item, dict):
            continue
        try:
            grams = float(item.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        ams = item.get("ams")
        if grams <= 0 or not isinstance(ams, int):
            continue
        slot = slot_from_flat(ams)
        spool_id = assignment.get(slot.key) if slot else None
        if factor is None:
            entries.append(Entry(job["id"], ams, spool_id, grams, "skipped", note="cancelled, progress unknown", created=now))
        elif spool_id is None:
            entries.append(Entry(job["id"], ams, None, grams, "skipped", note="no spool assigned to slot", created=now))
        else:
            total = round(grams * factor, 2)
            run = (runouts or {}).get(slot.key) or {}
            backup_id = assignment.get(run.get("backup"))
            if backup_id is None or backup_id == spool_id or run.get("progress") is None:
                entries.append(Entry(job["id"], ams, spool_id, total, "pending", created=now))
                continue
            first = min(total, round(grams * max(0, min(100, run["progress"])) / 100, 2))
            entries.append(Entry(job["id"], ams, spool_id, first, "pending", note=f"ran out at {run['progress']}%",
                                 created=now, zero=True))
            if (rest := round(total - first, 2)) > 0:
                entries.append(Entry(job["id"], ams, backup_id, rest, "pending", note=f"auto-refill from {slot.key}",
                                     created=now, share="backup"))
    return entries


@dataclass
class Ledger:
    entries: dict[str, Entry] = field(default_factory=dict)
    done_jobs: dict[str, float] = field(default_factory=dict)  # job id -> processed at
    snapshot: dict | None = None  # {"start_ts": float, "assignment": {slot_key: spool_id}}
    progress: int | None = None
    last_end_ts: float | None = None

    def add_job(self, job: dict, entries: list[Entry], now: float) -> bool:
        jid = str(job["id"])
        if jid in self.done_jobs:
            return False
        for entry in entries:
            self.entries[entry.key] = entry
        self.done_jobs[jid] = now
        end = parse_ts(job.get("endTime"))
        if end is not None and (self.last_end_ts is None or end > self.last_end_ts):
            self.last_end_ts = end
        return True

    def pending(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.state == "pending"]

    def prune(self, now: float) -> None:
        cutoff = now - LEDGER_TTL_DAYS * 86400
        self.entries = {k: e for k, e in self.entries.items() if e.created >= cutoff or e.state == "pending"}
        self.done_jobs = {k: t for k, t in self.done_jobs.items() if t >= cutoff}

    def to_dict(self) -> dict:
        return {"entries": {k: e.to_dict() for k, e in self.entries.items()},
                "done_jobs": self.done_jobs, "snapshot": self.snapshot,
                "progress": self.progress, "last_end_ts": self.last_end_ts}

    @classmethod
    def from_dict(cls, data: dict) -> Ledger:
        return cls(entries={k: Entry.from_dict(e) for k, e in (data.get("entries") or {}).items()},
                   done_jobs=dict(data.get("done_jobs") or {}), snapshot=data.get("snapshot"),
                   progress=data.get("progress"), last_end_ts=data.get("last_end_ts"))


async def apply_entry(entry: Entry, spoolman: Any, save: Callable[[], Awaitable[None]]) -> None:
    """Deduct once. `used_before` is persisted before writing so a retry can tell
    whether an uncertain (timed-out) write already landed."""
    spool = await spoolman.get_spool(entry.spool_id)
    if spool is None:
        entry.state, entry.note = "skipped", "spool deleted"
        await save()
        return
    used = float(spool.get("used_weight") or 0)
    landed = entry.used_before is not None and used >= entry.used_before + entry.grams - USED_WEIGHT_TOLERANCE_G
    if not landed:
        entry.used_before = used
        await save()
        await spoolman.use_weight(entry.spool_id, entry.grams)
        used += entry.grams
    # A spool that ran out is physically empty whatever the estimate said (setting the total is retry-safe).
    initial = float(spool.get("initial_weight") or (spool.get("filament") or {}).get("weight") or 0)
    if entry.zero and used < initial:
        await spoolman.patch_spool(entry.spool_id, {"used_weight": initial})
    entry.state = "applied"
    await save()
