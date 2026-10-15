# Spool Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Home Assistant custom integration (`spool_bridge`) that makes Spoolman the master filament inventory for one Bambu X2D, assigns scanned spools to AMS slots, sets tray profiles on the printer, mirrors spools/slots/grams into the Bambu cloud filament library, and deducts per-job grams.

**Architecture:** Pure, HA-free modules (`model`, `assignment`, `cloud_sync` planner, `usage` ledger, `migration` planner) hold all decision logic and are unit-tested directly. Thin adapters talk to the outside: `bambu_cloud` (blocking curl_cffi client, run in the executor), `spoolman` (aiohttp), `printer` (ha-bambulab entities/services). `bridge.py` wires HA events (tag scans, tray/status changes, Spoolman websocket, timers) to the logic.

**Tech Stack:** Python 3.14, Home Assistant 2026.10.1, `pytest-homeassistant-custom-component==0.13.372`, `curl_cffi>=0.13.0`, aiohttp (bundled with HA), Spoolman 0.27.0 (Docker image `ghcr.io/donkie/spoolman:0.27.0` for integration tests), uv.

**Spec:** `docs/superpowers/specs/2026-10-15-spool-bridge-design.md`

## Global Constraints

- Domain `spool_bridge`; HACS custom repository, public GitHub repo `d1ggs/ha-spool-bridge`.
- Only printer in scope: the X2D (any other `deviceId` in cloud data is ignored).
- Spoolman always wins; the bridge is the only writer to the Bambu cloud library.
- Spoolman extra fields: spool `bambu_cloud_id`, `bambu_rfid`, `bambu_slot`; filament `bambu_filament_id`. Values are JSON-encoded strings. The bridge never writes `active_tray` or `tag`.
- Slot key format: `ams:<unit>:<slot>` (0-based) or `ext:<n>` (0-based).
- Tag ids accepted: `spool-<id>` and `spool/show/<id>`.
- Load window 300 s; settle 10 s; reconcile every 15 min + 5 s debounce; ≥10 s between cloud writes per spool; ledger retry every 5 min; stuck notification after 1 h; ledger TTL 90 days; uncertain-write tolerance 0.5 g.
- Material default profile ids: PLA→GFL99, PETG→GFG99, ABS→GFB99, ASA→GFB98, TPU→GFU99, PLA-CF→GFL98, anything else→GFL99.
- Never commit real serials, RFIDs, spool/cloud ids, tokens or email. Fixtures go through `scripts/anonymize_fixtures.py`.
- Code reused from `Timmes123/ha-bambu-filaments` (MIT) keeps its attribution header.

## Review Focus

1. **A tray reports `unavailable`/`unknown` (printer offline, HA restart)** → must not count as "empty" (no unassign) nor as "loaded" (no capture). Pinned in Task 4 (`parse_tray_state`) and Task 9 (`test_unavailable_tray_does_not_unassign`).
2. **The same job's finish event arrives twice / HA restarts during job fetch** → grams deducted once. Pinned in Task 7 (`test_add_job_is_idempotent`) and Task 9 (`test_finish_twice_books_once`).
3. **A spool is scanned that is archived or does not exist** → notify "unknown spool", nothing armed. Pinned in Task 9 (`test_scan_unknown_spool`).
4. **Cloud returns a non-zero `code` for a slot binding** → logged as error, not treated as success, retried next reconcile. Pinned in Task 6 (`test_bind_failure_is_reported`).
5. **Spoolman spool without filament weight/remaining weight (nulls)** → cloud payload still valid (defaults 1000 g). Pinned in Task 1 (`test_cloud_payload_defaults`).

---

### Task 1: Scaffold + pure model

**Files:**
- Create: `pyproject.toml`, `hacs.json`, `LICENSE`, `.gitignore`
- Create: `custom_components/spool_bridge/__init__.py` (placeholder docstring only, replaced in Task 9), `manifest.json`, `const.py`, `model.py`
- Test: `tests/__init__.py`, `tests/conftest.py`, `tests/test_model.py`

**Interfaces:**
- Produces: `SlotRef(kind, unit, slot)` with `.key`, `.parse()`, `.label(name)`, `.flat_index`; `slot_from_flat(ams) -> SlotRef|None`; `tag_to_spool_id(tag) -> int|None`; `hex6(v) -> str`; `extra_get(entity, key) -> str|None`; `extra_value(v) -> str|None`; `default_filament_id(material) -> str`; `spool_filament_id(spool) -> str`; `cloud_payload(spool) -> dict`; `cloud_field_diff(wanted, cloud) -> dict`; `tray_color(hex) -> str`. Constants in `const.py`.

- [ ] **Step 1: Scaffold**

`pyproject.toml`:
```toml
[project]
name = "ha-spool-bridge"
version = "0.1.0"
requires-python = ">=3.14"

[dependency-groups]
dev = [
  "pytest-homeassistant-custom-component==0.13.372",
  "curl_cffi>=0.13.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["integration: needs a running Spoolman (env SPOOLMAN_URL)"]
```

`hacs.json`:
```json
{ "name": "Spool Bridge", "homeassistant": "2026.8.0", "render_readme": true }
```

`custom_components/spool_bridge/manifest.json`:
```json
{
  "domain": "spool_bridge",
  "name": "Spool Bridge",
  "codeowners": ["@d1ggs"],
  "config_flow": true,
  "dependencies": ["http"],
  "after_dependencies": ["bambu_lab", "mobile_app", "tag"],
  "documentation": "https://github.com/d1ggs/ha-spool-bridge",
  "integration_type": "service",
  "iot_class": "cloud_polling",
  "issue_tracker": "https://github.com/d1ggs/ha-spool-bridge/issues",
  "requirements": ["curl_cffi>=0.13.0"],
  "version": "0.1.0"
}
```

`.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, `captures/`.

`LICENSE`: MIT, "Copyright (c) 2026 Diego Piccinotti".

`tests/conftest.py`:
```python
"""Shared test setup."""
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
```

`const.py`:
```python
"""Constants for Spool Bridge."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "spool_bridge"

CONF_SPOOLMAN_URL = "spoolman_url"
CONF_PRINTER_SERIAL = "printer_serial"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_REGION = "region"
CONF_EMAIL = "email"
CONF_TOKEN = "token"

DEFAULT_SPOOLMAN_URL = "http://3b88f413-spoolman:8000"
BAMBULAB_DOMAIN = "bambu_lab"
BAMBU_FILAMENTS_DOMAIN = "bambu_filaments"

EXTRA_CLOUD_ID = "bambu_cloud_id"
EXTRA_RFID = "bambu_rfid"
EXTRA_SLOT = "bambu_slot"
EXTRA_FILAMENT_ID = "bambu_filament_id"

LOAD_WINDOW_S = 300
SETTLE_S = 10
RECONCILE_INTERVAL = timedelta(minutes=15)
RECONCILE_DEBOUNCE_S = 5
CLOUD_WRITE_MIN_INTERVAL_S = 10
LEDGER_RETRY_INTERVAL = timedelta(minutes=5)
JOB_FETCH_ATTEMPTS = 10
JOB_FETCH_DELAY_S = 60
JOB_MATCH_TOLERANCE_S = 300
LEDGER_TTL_DAYS = 90
STUCK_AFTER_S = 3600
USED_WEIGHT_TOLERANCE_G = 0.5

TASK_FINISHED = 2
TASK_FAILED = 3
BUSY_STATES = {"prepare", "running", "pause", "slicing", "init"}
END_STATES = {"finish", "failed"}

AMS_TYPE_BY_MODEL = {"AMS": 1, "AMS Lite": 2, "AMS 2 Pro": 3, "AMS HT": 4}

SIGNAL_STATE = f"{DOMAIN}_state"
ACTION_PREFIX = "SPOOLBRIDGE"
```

- [ ] **Step 2: Write failing tests** `tests/test_model.py`:
```python
from custom_components.spool_bridge.model import (
    SlotRef, cloud_field_diff, cloud_payload, default_filament_id, extra_get,
    extra_value, hex6, slot_from_flat, spool_filament_id, tag_to_spool_id, tray_color,
)


def test_slot_key_roundtrip():
    for slot in (SlotRef("ams", 1, 2), SlotRef("ext", 0)):
        assert SlotRef.parse(slot.key) == slot
    assert SlotRef("ams", 1, 2).key == "ams:1:2"
    assert SlotRef("ext", 1).key == "ext:1"
    assert SlotRef.parse("") is None
    assert SlotRef.parse("ams:x:1") is None
    assert SlotRef.parse("foo:1") is None


def test_slot_label_and_flat():
    assert SlotRef("ams", 1, 2).label("X2D") == "X2D AMS 2 · slot 3"
    assert SlotRef("ext", 0).label("X2D") == "X2D External 1"
    assert SlotRef("ams", 1, 2).flat_index == 6
    assert slot_from_flat(5) == SlotRef("ams", 1, 1)
    assert slot_from_flat(254) == SlotRef("ext", 0)
    assert slot_from_flat(255) == SlotRef("ext", 1)
    assert slot_from_flat(16) is None
    assert slot_from_flat(True) is None
    assert slot_from_flat("3") is None


def test_tag_to_spool_id():
    assert tag_to_spool_id("spool-42") == 42
    assert tag_to_spool_id("spool/show/7") == 7
    assert tag_to_spool_id("5f0ba733-172f") is None
    assert tag_to_spool_id(None) is None


def test_hex6_and_tray_color():
    assert hex6("#af7933") == "AF7933"
    assert hex6("AF7933FF") == "AF7933"
    assert hex6("red") == ""
    assert hex6(None) == ""
    assert tray_color("af7933") == "AF7933FF"
    assert tray_color(None) == "FFFFFFFF"


def test_extra_helpers():
    entity = {"extra": {"bambu_slot": '"ams:0:1"', "bad": "{", "empty": '""'}}
    assert extra_get(entity, "bambu_slot") == "ams:0:1"
    assert extra_get(entity, "bad") is None
    assert extra_get(entity, "empty") is None
    assert extra_get({}, "x") is None
    assert extra_value("ams:0:1") == '"ams:0:1"'
    assert extra_value(None) is None


def test_filament_id_defaults():
    assert default_filament_id("petg") == "GFG99"
    assert default_filament_id(None) == "GFL99"
    spool = {"filament": {"material": "PLA", "extra": {"bambu_filament_id": '"GFL03"'}}}
    assert spool_filament_id(spool) == "GFL03"


def _spool(**over):
    spool = {
        "id": 1,
        "remaining_weight": 412.4,
        "initial_weight": 1000,
        "filament": {
            "name": "PolyTerra PLA", "material": "PLA", "color_hex": "333333",
            "weight": 1000, "vendor": {"name": "Polymaker"}, "extra": {},
        },
        "extra": {},
    }
    spool.update(over)
    return spool


def test_cloud_payload():
    assert cloud_payload(_spool()) == {
        "filamentVendor": "Polymaker", "filamentType": "PLA", "filamentName": "PolyTerra PLA",
        "filamentId": "GFL99", "color": "#333333", "netWeight": 412, "totalNetWeight": 1000,
    }


def test_cloud_payload_defaults():
    spool = {"id": 2, "remaining_weight": None, "initial_weight": None,
             "filament": {"name": None, "material": None, "color_hex": None, "weight": None, "vendor": None}}
    assert cloud_payload(spool) == {
        "filamentVendor": "Generic", "filamentType": "PLA", "filamentName": "PLA",
        "filamentId": "GFL99", "color": "#FFFFFF", "netWeight": 1000, "totalNetWeight": 1000,
    }


def test_cloud_field_diff():
    wanted = cloud_payload(_spool())
    cloud = {"filamentVendor": "Polymaker", "filamentType": "PLA", "filamentName": "PolyTerra PLA",
             "filamentId": "GFL99", "color": "#333333FF", "netWeight": 412, "totalNetWeight": 1000}
    assert cloud_field_diff(wanted, cloud) == {}
    cloud["netWeight"] = 450
    cloud["color"] = "#000000"
    assert cloud_field_diff(wanted, cloud) == {"netWeight": 412, "color": "#333333"}
```

- [ ] **Step 3: Run to verify failure**

Run: `uv sync && uv run pytest tests/test_model.py -q`
Expected: FAIL — `ModuleNotFoundError: custom_components.spool_bridge.model`.

- [ ] **Step 4: Implement** `custom_components/spool_bridge/model.py`:
```python
"""Pure data helpers shared by all modules (no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

MATERIAL_DEFAULT_IDS = {
    "PLA": "GFL99", "PETG": "GFG99", "ABS": "GFB99",
    "ASA": "GFB98", "TPU": "GFU99", "PLA-CF": "GFL98",
}
FALLBACK_FILAMENT_ID = "GFL99"


@dataclass(frozen=True, order=True)
class SlotRef:
    """One physical spool position: an AMS slot or an external holder."""

    kind: str  # "ams" | "ext"
    unit: int  # AMS index (0-based) or external holder index (0-based)
    slot: int = 0  # slot within the AMS (0-based); 0 for external holders

    @property
    def key(self) -> str:
        if self.kind == "ams":
            return f"ams:{self.unit}:{self.slot}"
        return f"ext:{self.unit}"

    @classmethod
    def parse(cls, value: str | None) -> SlotRef | None:
        if not value:
            return None
        parts = value.split(":")
        try:
            if parts[0] == "ams" and len(parts) == 3:
                return cls("ams", int(parts[1]), int(parts[2]))
            if parts[0] == "ext" and len(parts) == 2:
                return cls("ext", int(parts[1]))
        except ValueError:
            return None
        return None

    def label(self, printer_name: str) -> str:
        if self.kind == "ams":
            return f"{printer_name} AMS {self.unit + 1} · slot {self.slot + 1}"
        return f"{printer_name} External {self.unit + 1}"

    @property
    def flat_index(self) -> int:
        """Bambu's flat tray index as used in cloud print jobs."""
        if self.kind == "ams":
            return self.unit * 4 + self.slot
        return 254 + self.unit


def slot_from_flat(ams: Any) -> SlotRef | None:
    """Cloud job `amsDetailMapping[].ams` -> slot (AMS HT units are out of scope)."""
    if isinstance(ams, bool) or not isinstance(ams, int):
        return None
    if 0 <= ams < 16:
        return SlotRef("ams", ams // 4, ams % 4)
    if ams in (254, 255):
        return SlotRef("ext", ams - 254)
    return None


_TAG_RE = re.compile(r"^(?:spool-(\d+)|spool/show/(\d+))$")


def tag_to_spool_id(tag_id: str | None) -> int | None:
    """Spoolman spool id from an HA tag id (`spool-42` or `spool/show/42`)."""
    match = _TAG_RE.match((tag_id or "").strip())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?$")


def hex6(value: Any) -> str:
    """Bare upper-case RRGGBB, or "" for anything that is not a hex colour."""
    if not isinstance(value, str):
        return ""
    match = _HEX_RE.match(value.strip())
    return match.group(1).upper() if match else ""


def tray_color(color_hex: str | None) -> str:
    """Printer tray colour: RRGGBBAA."""
    return (hex6(color_hex) or "FFFFFF") + "FF"


def extra_get(entity: dict, key: str) -> str | None:
    """Decoded Spoolman extra field value ("" and null count as unset)."""
    raw = (entity.get("extra") or {}).get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if value is None or value == "":
        return None
    return str(value)


def extra_value(value: str | None) -> str | None:
    """Encode for a Spoolman PATCH; None removes the key."""
    return None if value is None else json.dumps(str(value))


def default_filament_id(material: str | None) -> str:
    return MATERIAL_DEFAULT_IDS.get((material or "").strip().upper(), FALLBACK_FILAMENT_ID)


def spool_filament_id(spool: dict) -> str:
    filament = spool.get("filament") or {}
    return extra_get(filament, "bambu_filament_id") or default_filament_id(filament.get("material"))


def cloud_payload(spool: dict) -> dict:
    """Bambu cloud library fields for a Spoolman spool (no id)."""
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or "Generic"
    material = filament.get("material") or "PLA"
    total = spool.get("initial_weight") or filament.get("weight") or 1000
    remaining = spool.get("remaining_weight")
    if remaining is None:
        remaining = total
    color = hex6(filament.get("color_hex")) or "FFFFFF"
    return {
        "filamentVendor": vendor,
        "filamentType": material,
        "filamentName": filament.get("name") or material,
        "filamentId": spool_filament_id(spool),
        "color": f"#{color}",
        "netWeight": max(0, int(round(float(remaining)))),
        "totalNetWeight": max(1, int(round(float(total)))),
    }


COMPARED_FIELDS = (
    "filamentVendor", "filamentType", "filamentName", "filamentId",
    "color", "netWeight", "totalNetWeight",
)


def cloud_field_diff(wanted: dict, cloud: dict) -> dict:
    """Fields of `wanted` that differ from the cloud spool."""
    diff: dict = {}
    for key in COMPARED_FIELDS:
        have, want = cloud.get(key), wanted[key]
        if key == "color":
            if hex6(have) != hex6(want):
                diff[key] = want
        elif key in ("netWeight", "totalNetWeight"):
            try:
                same = int(have) == want
            except (TypeError, ValueError):
                same = False
            if not same:
                diff[key] = want
        elif (have or "") != want:
            diff[key] = want
    return diff
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_model.py -q` → PASS.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: scaffold and pure model helpers"`

---

### Task 2: Bambu cloud client + anonymised fixtures

**Files:**
- Create: `custom_components/spool_bridge/bambu_cloud.py`
- Create: `scripts/anonymize_fixtures.py`, `tests/fixtures/cloud_spools.json`, `tests/fixtures/cloud_tasks.json`
- Test: `tests/test_bambu_cloud.py`

**Interfaces:**
- Produces: exceptions `BambuCloudError`, `CloudflareBlocked`, `AuthFailed`, `AuthExpired`, `EmailCodeRequired`, `CodeIncorrect`, `TfaRequired(tfa_key)`; `BambuCloudClient(region, token=None)` with `.token`, `login(email, password) -> str`, `request_code(email)`, `login_with_code(email, code) -> str`, `login_with_tfa(tfa_key, code) -> str`, `get_spools() -> list[dict]`, `create_spool(payload) -> None`, `update_spool(body) -> dict`, `delete_spools(ids) -> None`, `ams_sync(dev_id, items) -> dict`, `slot_mappings_sync(dev_id, mappings) -> list[dict]`, `get_tasks(limit=20) -> list[dict]`. All blocking.

- [ ] **Step 1: Anonymiser** `scripts/anonymize_fixtures.py` — reads a raw capture (`{"hits": [...]}`) and writes a fixture keeping only the keys the bridge uses, with serials/RFIDs/ids replaced deterministically:
```python
"""Anonymise raw Bambu cloud captures into test fixtures.

usage: python scripts/anonymize_fixtures.py spools|tasks RAW.json OUT.json
"""
import hashlib
import json
import sys

SPOOL_KEYS = ["id", "createType", "filamentVendor", "filamentType", "filamentName", "filamentId",
              "RFID", "color", "colors", "netWeight", "totalNetWeight", "inPrinter", "devId",
              "amsSn", "amsId", "slotId", "amsType", "deviceName", "note", "displayName"]
TASK_KEYS = ["id", "deviceId", "deviceModel", "status", "startTime", "endTime", "weight", "amsDetailMapping"]
SERIAL_KEYS = {"devId", "deviceId", "amsSn"}


def fake(value: str, length: int, prefix: str = "") -> str:
    digest = hashlib.sha256(value.encode()).hexdigest().upper()
    return (prefix + digest)[:length] if value else value


def main() -> None:
    kind, raw_path, out_path = sys.argv[1:4]
    raw = json.load(open(raw_path))
    keys = SPOOL_KEYS if kind == "spools" else TASK_KEYS
    ids: dict[int, int] = {}
    out = []
    for hit in raw["hits"]:
        item = {k: hit.get(k) for k in keys if k in hit}
        item["id"] = ids.setdefault(hit["id"], 1000 + len(ids))
        for k in SERIAL_KEYS & item.keys():
            if item[k]:
                item[k] = fake(item[k], len(item[k]), "FAKE")
        if item.get("RFID"):
            item["RFID"] = fake(item["RFID"], 32)
        out.append(item)
    json.dump({"hits": out}, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
```
Generate: `python3 scripts/anonymize_fixtures.py spools captures/spools_before.json tests/fixtures/cloud_spools.json` and the same for `tasks` (raw captures copied from the probe directory into the git-ignored `captures/`). Verify the fixtures contain none of the real serials (grep for them) before committing.

- [ ] **Step 2: Failing tests** `tests/test_bambu_cloud.py`:
```python
import json
from pathlib import Path

import pytest

from custom_components.spool_bridge import bambu_cloud
from custom_components.spool_bridge.bambu_cloud import (
    AuthExpired, BambuCloudClient, BambuCloudError, CloudflareBlocked,
)

FIX = Path(__file__).parent / "fixtures"


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body or {})
        self.cookies = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def calls(monkeypatch):
    recorded = []
    replies = []

    def make(method):
        def _call(url, headers=None, json=None, timeout=None, impersonate=None):
            recorded.append((method, url, json, headers))
            return replies.pop(0)
        return _call

    for m in ("get", "post", "put", "delete"):
        monkeypatch.setattr(bambu_cloud.curl_requests, m, make(m))
    return recorded, replies


def test_get_spools_paginates(calls):
    recorded, replies = calls
    hits = json.loads((FIX / "cloud_spools.json").read_text())["hits"]
    replies += [Resp(body={"hits": hits[:2], "total": 3}), Resp(body={"hits": hits[2:3], "total": 3})]
    spools = BambuCloudClient("global", "tok").get_spools()
    assert [s["id"] for s in spools] == [h["id"] for h in hits[:3]]
    assert "offset=2" in recorded[1][1]
    assert recorded[0][3]["Authorization"] == "Bearer tok"


def test_slot_mappings_sync_body(calls):
    recorded, replies = calls
    replies.append(Resp(body={"results": [{"amsSn": "A", "slotId": "0", "code": 0}]}))
    mapping = {"amsId": 1, "amsSn": "A", "amsType": 3, "rfid": "", "slotId": "0", "spoolId": 7}
    results = BambuCloudClient("global", "tok").slot_mappings_sync("DEV", [mapping])
    assert results == [{"amsSn": "A", "slotId": "0", "code": 0}]
    method, url, body, _ = recorded[0]
    assert method == "post"
    assert url.endswith("/v1/design-user-service/my/filament/v2/slot-mappings/sync")
    assert body == {"devId": "DEV", "mappings": [mapping]}


def test_get_tasks(calls):
    _, replies = calls
    tasks = json.loads((FIX / "cloud_tasks.json").read_text())["hits"]
    replies.append(Resp(body={"hits": tasks}))
    assert BambuCloudClient("global", "tok").get_tasks(15) == tasks


def test_401_is_auth_expired(calls):
    _, replies = calls
    replies.append(Resp(status=401, text="unauthorized"))
    with pytest.raises(AuthExpired):
        BambuCloudClient("global", "tok").get_spools()


def test_cloudflare_block(calls):
    _, replies = calls
    replies.append(Resp(status=403, text="<html>cloudflare</html>"))
    with pytest.raises(CloudflareBlocked):
        BambuCloudClient("global", "tok").get_tasks()


def test_non_json_is_cloud_error(calls):
    _, replies = calls
    replies.append(Resp(status=200, body=None, text="oops"))
    with pytest.raises(BambuCloudError):
        BambuCloudClient("global", "tok").get_tasks()
```
Run `uv run pytest tests/test_bambu_cloud.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement** `bambu_cloud.py`: copy `api.py` from `Timmes123/ha-bambu-filaments` v1.8.4 (`/var/tmp/scratch/hbf/tag184/custom_components/bambu_filaments/api.py`) verbatim, then:
  - Prepend the attribution header:
    ```python
    """Blocking client for the Bambu Lab cloud filament library and print jobs.

    Login, request plumbing and the filament endpoints are adapted from
    ha-bambu-filaments by Timmes123 (MIT License,
    https://github.com/Timmes123/ha-bambu-filaments). Slot mappings and task
    selection were added for Spool Bridge. Must be called from an executor.
    """
    ```
  - Remove `get_catalog` and `get_user_presets` (unused, YAGNI).
  - Change `get_tasks` default `limit` to 20.
  - Add:
    ```python
    def slot_mappings_sync(self, dev_id: str, mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bind (spoolId>0) or unbind (spoolId 0, rfid "") spools to printer slots.

        Same call Bambu Studio's filament manager makes. Each result carries
        `code` (0 = applied).
        """
        response = self._request(
            "post",
            f"{self._api}/v1/design-user-service/my/filament/v2/slot-mappings/sync",
            body={"devId": dev_id, "mappings": mappings},
        )
        if response.status_code != 200:
            raise BambuCloudError(f"Slot mapping sync failed (HTTP {response.status_code})")
        return [r for r in (self._json(response).get("results") or []) if isinstance(r, dict)]
    ```
- [ ] **Step 4:** `uv run pytest tests/test_bambu_cloud.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: Bambu cloud client with slot mappings"`

---

### Task 3: Spoolman client

**Files:**
- Create: `custom_components/spool_bridge/spoolman.py`
- Test: `tests/test_spoolman_integration.py` (marked `integration`, runs against a real Spoolman)

**Interfaces:**
- Produces: `SpoolmanError`; `SpoolmanClient(session: aiohttp.ClientSession, base_url: str)` with async `info() -> dict`, `ensure_extra_fields() -> None`, `get_spools(allow_archived=True) -> list[dict]`, `get_spool(spool_id) -> dict|None`, `patch_spool(spool_id, body) -> dict`, `set_spool_extra(spool_id, values: dict[str, str|None], location: str|None|object=KEEP) -> dict`, `use_weight(spool_id, grams) -> dict`, `ensure_vendor(name) -> int`, `ensure_filament(vendor_id, *, name, material, color_hex, weight, spool_weight, bambu_filament_id) -> int`, `create_spool(filament_id, *, initial_weight, remaining_weight, extra, location) -> dict`, `listen(on_event: Callable[[dict], None]) -> None` (runs until cancelled, reconnects every 10 s). Module constant `KEEP` (sentinel).

- [ ] **Step 1: Failing integration test** `tests/test_spoolman_integration.py`:
```python
import os

import aiohttp
import pytest

from custom_components.spool_bridge.spoolman import SpoolmanClient

URL = os.environ.get("SPOOLMAN_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="SPOOLMAN_URL not set")]


@pytest.fixture
async def client():
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
```
Start Spoolman: `docker run -d --rm --name spoolman-test -p 7913:8000 ghcr.io/donkie/spoolman:0.27.0`, then `SPOOLMAN_URL=http://localhost:7913 uv run pytest tests/test_spoolman_integration.py -q` → FAIL (module missing).

- [ ] **Step 2: Implement** `spoolman.py`:
```python
"""Async client for the Spoolman REST API and change websocket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

import aiohttp

from .const import EXTRA_CLOUD_ID, EXTRA_FILAMENT_ID, EXTRA_RFID, EXTRA_SLOT
from .model import extra_value, hex6

_LOGGER = logging.getLogger(__name__)

KEEP = object()

EXTRA_FIELDS = (
    ("spool", EXTRA_CLOUD_ID, "Bambu cloud id"),
    ("spool", EXTRA_RFID, "Bambu RFID"),
    ("spool", EXTRA_SLOT, "Bambu slot"),
    ("filament", EXTRA_FILAMENT_ID, "Bambu filament profile id"),
)
DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21}


class SpoolmanError(Exception):
    """Spoolman unreachable or returned an error."""


class SpoolmanClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")

    async def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}/api/v1{path}"
        try:
            async with self._session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=15), **kwargs
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    raise SpoolmanError(f"{method} {path}: HTTP {resp.status}: {await resp.text()}")
                if resp.status == 204:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SpoolmanError(f"{method} {path}: {err}") from err

    async def info(self) -> dict:
        result = await self._req("GET", "/info")
        if not isinstance(result, dict):
            raise SpoolmanError("Not a Spoolman server")
        return result

    async def ensure_extra_fields(self) -> None:
        for entity, key, name in EXTRA_FIELDS:
            existing = await self._req("GET", f"/field/{entity}") or []
            if not any(f.get("key") == key for f in existing):
                await self._req("POST", f"/field/{entity}/{key}", json={"name": name, "field_type": "text"})

    async def get_spools(self, allow_archived: bool = True) -> list[dict]:
        params = {"allow_archived": "true" if allow_archived else "false"}
        return list(await self._req("GET", "/spool", params=params) or [])

    async def get_spool(self, spool_id: int) -> dict | None:
        return await self._req("GET", f"/spool/{int(spool_id)}")

    async def patch_spool(self, spool_id: int, body: dict) -> dict:
        result = await self._req("PATCH", f"/spool/{int(spool_id)}", json=body)
        if result is None:
            raise SpoolmanError(f"Spool {spool_id} not found")
        return result

    async def set_spool_extra(
        self, spool_id: int, values: dict[str, str | None], location: Any = KEEP
    ) -> dict:
        """Set/remove only the given extra keys (Spoolman merges per key)."""
        body: dict[str, Any] = {"extra": {k: extra_value(v) for k, v in values.items()}}
        if location is not KEEP:
            body["location"] = location
        return await self.patch_spool(spool_id, body)

    async def use_weight(self, spool_id: int, grams: float) -> dict:
        result = await self._req("PUT", f"/spool/{int(spool_id)}/use", json={"use_weight": float(grams)})
        if result is None:
            raise SpoolmanError(f"Spool {spool_id} not found")
        return result

    async def ensure_vendor(self, name: str) -> int:
        for vendor in await self._req("GET", "/vendor") or []:
            if (vendor.get("name") or "").strip().lower() == name.strip().lower():
                return vendor["id"]
        return (await self._req("POST", "/vendor", json={"name": name}))["id"]

    async def ensure_filament(
        self, vendor_id: int, *, name: str, material: str, color_hex: str,
        weight: float, spool_weight: float | None, bambu_filament_id: str | None,
    ) -> int:
        color = hex6(color_hex)
        for fil in await self._req("GET", "/filament") or []:
            if (
                (fil.get("vendor") or {}).get("id") == vendor_id
                and (fil.get("name") or "") == name
                and (fil.get("material") or "") == material
                and hex6(fil.get("color_hex")) == color
            ):
                return fil["id"]
        body = {
            "vendor_id": vendor_id, "name": name, "material": material,
            "color_hex": color or None, "weight": weight, "spool_weight": spool_weight,
            "density": DENSITY.get(material.upper(), 1.24), "diameter": 1.75,
            "extra": {EXTRA_FILAMENT_ID: extra_value(bambu_filament_id)} if bambu_filament_id else {},
        }
        return (await self._req("POST", "/filament", json=body))["id"]

    async def create_spool(
        self, filament_id: int, *, initial_weight: float, remaining_weight: float,
        extra: dict[str, str | None], location: str | None,
    ) -> dict:
        body = {
            "filament_id": filament_id, "initial_weight": initial_weight,
            "remaining_weight": remaining_weight, "location": location,
            "extra": {k: extra_value(v) for k, v in extra.items() if v is not None},
        }
        return await self._req("POST", "/spool", json=body)

    async def listen(self, on_event: Callable[[dict], None]) -> None:
        """Forward spool change events until cancelled; reconnect on errors."""
        ws_url = self._base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/v1/spool"
        while True:
            try:
                async with self._session.ws_connect(ws_url, heartbeat=30) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                on_event(msg.json())
                            except ValueError:
                                continue
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Spoolman websocket error: %s", err)
            await asyncio.sleep(10)
```
- [ ] **Step 3:** Integration test → PASS. Stop container: `docker stop spoolman-test`.
- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat: Spoolman client"`

---

### Task 4: Printer adapter

**Files:**
- Create: `custom_components/spool_bridge/printer.py`
- Test: `tests/test_printer.py`

**Interfaces:**
- Consumes: `SlotRef`, `tray_color` (Task 1); `AMS_TYPE_BY_MODEL`, `BAMBULAB_DOMAIN` (const).
- Produces: `TrayInfo(entity_id, slot: SlotRef, ams_serial: str, ams_type: int)`; `TrayState(empty, rfid, filament_id, material, color, name, tray_weight, remain)`; `parse_tray_state(state) -> TrayState|None` (None when unavailable/unknown/missing); `Printer(hass, serial)` with `name() -> str`, `trays() -> list[TrayInfo]`, `tray_for(slot) -> TrayInfo|None`, `tray_by_entity(entity_id) -> TrayInfo|None`, `tray_state(tray) -> TrayState|None`, `status_entity -> str|None`, `progress_entity -> str|None`, `status() -> str|None`, `progress() -> int|None`, `async set_filament(tray, *, filament_id, color_hex, material, nozzle_temp) -> None`.

- [ ] **Step 1: Failing tests** `tests/test_printer.py`:
```python
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spool_bridge.model import SlotRef
from custom_components.spool_bridge.printer import Printer, parse_tray_state

SERIAL = "FAKEPRINTER0001"


def setup_printer(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="bambu_lab")
    entry.add_to_hass(hass)
    devs, ents = dr.async_get(hass), er.async_get(hass)
    printer = devs.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("bambu_lab", SERIAL)},
                                       name=f"X2D_{SERIAL}", model="X2D")
    for n, ams_sn in ((1, "FAKEAMS000000001"), (2, "FAKEAMS000000002")):
        ams = devs.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("bambu_lab", ams_sn)},
                                       name=f"X2D_{SERIAL}_AMS_{n}", model="AMS 2 Pro", via_device=("bambu_lab", SERIAL))
        for t in range(1, 5):
            ent = ents.async_get_or_create("sensor", "bambu_lab", f"X2D_{SERIAL}_AMS_{ams_sn}_tray_{t}",
                                           device_id=ams.id, suggested_object_id=f"ams_{n}_slot_{t}")
            hass.states.async_set(ent.entity_id, "Generic PLA", {"empty": False, "tray_uuid": "0" * 32,
                                                                 "type": "PLA", "color": "#AF7933FF"})
    for suffix in ("", "2"):
        ents.async_get_or_create("sensor", "bambu_lab", f"X2D_{SERIAL}_ExternalSpool{suffix}_external_spool",
                                 device_id=printer.id, suggested_object_id=f"ext{suffix}")
    st = ents.async_get_or_create("sensor", "bambu_lab", f"{SERIAL}_print_status", device_id=printer.id,
                                  suggested_object_id="status")
    pr = ents.async_get_or_create("sensor", "bambu_lab", f"{SERIAL}_print_progress", device_id=printer.id,
                                  suggested_object_id="progress")
    hass.states.async_set(st.entity_id, "running")
    hass.states.async_set(pr.entity_id, "37")


async def test_trays_and_status(hass):
    setup_printer(hass)
    p = Printer(hass, SERIAL)
    trays = p.trays()
    assert [t.slot.key for t in trays] == [
        "ams:0:0", "ams:0:1", "ams:0:2", "ams:0:3", "ams:1:0", "ams:1:1", "ams:1:2", "ams:1:3", "ext:0", "ext:1"]
    t = p.tray_for(SlotRef("ams", 1, 2))
    assert t.ams_serial == "FAKEAMS000000002" and t.ams_type == 3
    assert p.tray_for(SlotRef("ext", 1)).ams_serial == ""
    assert p.tray_by_entity(t.entity_id) == t
    assert p.status() == "running" and p.progress() == 37
    assert p.tray_state(t).rfid is None


def test_parse_tray_state():
    class S:
        def __init__(self, state, attrs):
            self.state, self.attributes = state, attrs

    assert parse_tray_state(None) is None
    assert parse_tray_state(S("unavailable", {})) is None
    assert parse_tray_state(S("unknown", {"empty": True})) is None
    s = parse_tray_state(S("Bambu PLA Basic", {"empty": False, "tray_uuid": "abcdef0123" + "1" * 22,
                                                "filament_id": "GFA00", "type": "PLA", "remain": 80,
                                                "tray_weight": "1000", "name": "Bambu PLA Basic"}))
    assert s.rfid == "ABCDEF0123" + "1" * 22 and s.remain == 80 and s.tray_weight == 1000 and not s.empty


async def test_set_filament_calls_bambulab(hass):
    setup_printer(hass)
    calls: list[ServiceCall] = []

    async def handler(call):
        calls.append(call)

    hass.services.async_register("bambu_lab", "set_filament", handler)
    p = Printer(hass, SERIAL)
    tray = p.tray_for(SlotRef("ams", 0, 0))
    await p.set_filament(tray, filament_id="GFL03", color_hex="006633", material="PLA", nozzle_temp=215)
    assert calls[0].data == {"entity_id": tray.entity_id, "tray_info_idx": "GFL03", "tray_color": "006633FF",
                             "tray_type": "PLA", "nozzle_temp_min": 205, "nozzle_temp_max": 225}
```
Run → FAIL (module missing).

- [ ] **Step 2: Implement** `printer.py`:
```python
"""The X2D as seen through ha-bambulab: trays, print status, tray profile writes."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import AMS_TYPE_BY_MODEL, BAMBULAB_DOMAIN
from .model import SlotRef, tray_color

_AMS_TRAY_RE = re.compile(r"^[^_]+_(?P<serial>[^_]+)_AMS_(?P<ams>[^_]+)_tray_(?P<n>\d+)$")
_EXT_RE = re.compile(r"^[^_]+_(?P<serial>[^_]+)_ExternalSpool(?P<n>\d*)_external_spool$")
_AMS_INDEX_RE = re.compile(r"_AMS_(?P<n>\d+)$")
_HEX32 = re.compile(r"^[0-9A-F]{32}$")


@dataclass(frozen=True)
class TrayInfo:
    entity_id: str
    slot: SlotRef
    ams_serial: str  # "" for external holders
    ams_type: int  # cloud amsType; 0 for external holders


@dataclass(frozen=True)
class TrayState:
    empty: bool
    rfid: str | None  # Bambu tray UUID (32 hex) for official spools
    filament_id: str
    material: str
    color: str
    name: str
    tray_weight: int
    remain: int  # percent, -1 unknown


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_tray_state(state: Any) -> TrayState | None:
    """None when the sensor is missing or unavailable - never treat that as empty."""
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    attrs = state.attributes
    uuid = str(attrs.get("tray_uuid") or "").upper()
    rfid = uuid if _HEX32.match(uuid) and uuid.strip("0") else None
    return TrayState(
        empty=bool(attrs.get("empty")),
        rfid=rfid,
        filament_id=str(attrs.get("filament_id") or ""),
        material=str(attrs.get("type") or ""),
        color=str(attrs.get("color") or ""),
        name=str(attrs.get("name") or ""),
        tray_weight=_int(attrs.get("tray_weight"), 0),
        remain=_int(attrs.get("remain"), -1),
    )


class Printer:
    def __init__(self, hass: HomeAssistant, serial: str) -> None:
        self.hass = hass
        self.serial = serial

    def _entity(self, unique_id: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("sensor", BAMBULAB_DOMAIN, unique_id)

    @property
    def status_entity(self) -> str | None:
        return self._entity(f"{self.serial}_print_status")

    @property
    def progress_entity(self) -> str | None:
        return self._entity(f"{self.serial}_print_progress")

    def name(self) -> str:
        device = dr.async_get(self.hass).async_get_device(identifiers={(BAMBULAB_DOMAIN, self.serial)})
        return (device and (device.name_by_user or device.model)) or "Printer"

    def trays(self) -> list[TrayInfo]:
        ent_reg, dev_reg = er.async_get(self.hass), dr.async_get(self.hass)
        found: list[TrayInfo] = []
        for entry in ent_reg.entities.values():
            if entry.platform != BAMBULAB_DOMAIN or entry.domain != "sensor" or entry.disabled:
                continue
            uid = entry.unique_id or ""
            if (m := _AMS_TRAY_RE.match(uid)) and m.group("serial") == self.serial:
                device = dev_reg.async_get(entry.device_id) if entry.device_id else None
                index = _AMS_INDEX_RE.search(device.name or "") if device else None
                if index is None:
                    continue
                found.append(TrayInfo(
                    entry.entity_id,
                    SlotRef("ams", int(index.group("n")) - 1, int(m.group("n")) - 1),
                    m.group("ams"),
                    AMS_TYPE_BY_MODEL.get(device.model or "", 0),
                ))
            elif (m := _EXT_RE.match(uid)) and m.group("serial") == self.serial:
                found.append(TrayInfo(entry.entity_id, SlotRef("ext", int(m.group("n") or 1) - 1), "", 0))
        return sorted(found, key=lambda t: t.slot)

    def tray_for(self, slot: SlotRef) -> TrayInfo | None:
        return next((t for t in self.trays() if t.slot == slot), None)

    def tray_by_entity(self, entity_id: str) -> TrayInfo | None:
        return next((t for t in self.trays() if t.entity_id == entity_id), None)

    def tray_state(self, tray: TrayInfo) -> TrayState | None:
        return parse_tray_state(self.hass.states.get(tray.entity_id))

    def status(self) -> str | None:
        state = self.hass.states.get(self.status_entity) if self.status_entity else None
        return state.state if state else None

    def progress(self) -> int | None:
        state = self.hass.states.get(self.progress_entity) if self.progress_entity else None
        value = _int(state.state, -1) if state else -1
        return value if value >= 0 else None

    async def set_filament(
        self, tray: TrayInfo, *, filament_id: str, color_hex: str | None,
        material: str | None, nozzle_temp: int | None,
    ) -> None:
        low, high = (nozzle_temp - 10, nozzle_temp + 10) if nozzle_temp else (190, 240)
        await self.hass.services.async_call(
            BAMBULAB_DOMAIN, "set_filament",
            {
                "entity_id": tray.entity_id, "tray_info_idx": filament_id,
                "tray_color": tray_color(color_hex), "tray_type": material or "PLA",
                "nozzle_temp_min": low, "nozzle_temp_max": high,
            },
            blocking=True,
        )
```
- [ ] **Step 3:** `uv run pytest tests/test_printer.py -q` → PASS.
- [ ] **Step 4: Commit** — `git commit -am "feat: printer adapter"` (after `git add -A`).

---

### Task 5: Load window (scan → load)

**Files:**
- Create: `custom_components/spool_bridge/assignment.py`
- Test: `tests/test_assignment.py`

**Interfaces:**
- Produces: decisions `Assign(spool_id, slot)`, `Ambiguous(spool_id, slots: tuple[SlotRef,...])`, `Expired(spool_id)`; `LoadWindow(window_s, settle_s)` with `scan(spool_id, now)`, `armed -> int|None`, `tray_loaded(slot, now) -> bool`, `settle(now) -> Assign|Ambiguous|None`, `expire(now) -> Expired|None`, `cancel()`.

- [ ] **Step 1: Failing tests** `tests/test_assignment.py`:
```python
from custom_components.spool_bridge.assignment import Ambiguous, Assign, Expired, LoadWindow
from custom_components.spool_bridge.model import SlotRef

A, B = SlotRef("ams", 0, 1), SlotRef("ams", 1, 2)


def test_single_load_assigns_after_settle():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    assert w.tray_loaded(A, now=20)
    assert w.settle(now=25) is None
    assert w.settle(now=30) == Assign(42, A)
    assert w.armed is None


def test_two_loads_are_ambiguous():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=20)
    w.tray_loaded(B, now=22)
    assert w.settle(now=31) == Ambiguous(42, (A, B))


def test_load_without_scan_ignored():
    w = LoadWindow(300, 10)
    assert not w.tray_loaded(A, now=1)
    assert w.settle(now=100) is None


def test_load_after_window_ignored_and_expires():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    assert not w.tray_loaded(A, now=301)
    assert w.expire(now=301) == Expired(42)
    assert w.armed is None


def test_expire_not_while_settling():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=299)
    assert w.expire(now=305) is None
    assert w.settle(now=309) == Assign(42, A)


def test_rescan_replaces():
    w = LoadWindow(300, 10)
    w.scan(42, now=0)
    w.tray_loaded(A, now=5)
    w.scan(43, now=6)
    assert w.settle(now=20) is None  # candidates reset by the new scan
    w.tray_loaded(B, now=21)
    assert w.settle(now=31) == Assign(43, B)
```
Run → FAIL.

- [ ] **Step 2: Implement** `assignment.py`:
```python
"""Which tray did the scanned spool go into? (pure logic)"""

from __future__ import annotations

from dataclasses import dataclass

from .model import SlotRef


@dataclass(frozen=True)
class Assign:
    spool_id: int
    slot: SlotRef


@dataclass(frozen=True)
class Ambiguous:
    spool_id: int
    slots: tuple[SlotRef, ...]


@dataclass(frozen=True)
class Expired:
    spool_id: int


class LoadWindow:
    """One armed spool; the trays that went empty -> loaded while it was armed.

    The first load starts a settle period; loads inside it make the result
    ambiguous instead of guessing.
    """

    def __init__(self, window_s: float, settle_s: float) -> None:
        self.window_s = window_s
        self.settle_s = settle_s
        self._reset()

    def _reset(self) -> None:
        self._spool: int | None = None
        self._armed_at = 0.0
        self._first_at: float | None = None
        self._candidates: list[SlotRef] = []

    @property
    def armed(self) -> int | None:
        return self._spool

    def scan(self, spool_id: int, now: float) -> None:
        self._reset()
        self._spool = spool_id
        self._armed_at = now

    def cancel(self) -> None:
        self._reset()

    def tray_loaded(self, slot: SlotRef, now: float) -> bool:
        if self._spool is None:
            return False
        if self._first_at is None and now > self._armed_at + self.window_s:
            return False
        if self._first_at is None:
            self._first_at = now
        if slot not in self._candidates:
            self._candidates.append(slot)
        return True

    def settle(self, now: float) -> Assign | Ambiguous | None:
        if self._spool is None or self._first_at is None or now < self._first_at + self.settle_s:
            return None
        spool, slots = self._spool, tuple(self._candidates)
        self._reset()
        return Assign(spool, slots[0]) if len(slots) == 1 else Ambiguous(spool, slots)

    def expire(self, now: float) -> Expired | None:
        if self._spool is not None and self._first_at is None and now >= self._armed_at + self.window_s:
            spool = self._spool
            self._reset()
            return Expired(spool)
        return None
```
- [ ] **Step 3:** tests → PASS. **Step 4:** commit `feat: scan-to-load window`.

---

### Task 6: Cloud sync planner + executor

**Files:**
- Create: `custom_components/spool_bridge/cloud_sync.py`
- Test: `tests/test_cloud_sync.py`

**Interfaces:**
- Consumes: `cloud_payload`, `cloud_field_diff`, `extra_get`, `SlotRef` (Task 1); `BambuCloudClient`, `AuthExpired`, `BambuCloudError` (Task 2); `SpoolmanClient` (Task 3); `Printer`, `TrayInfo`, `TrayState` (Task 4).
- Produces: actions `Create(spool_id, payload)`, `Update(spool_id, cloud_id, fields)`, `Delete(spool_id, cloud_id)`, `Link(spool_id, cloud_id)`, `Bind(cloud_id, slot, rfid)`, `Unbind(slot)`; `plan(spools, cloud, dev_id) -> list`; `ams_sync_item(tray: TrayInfo, state: TrayState, payload: dict) -> dict`; class `CloudSync(hass, client, spoolman, printer, dev_id)` with `async reconcile() -> list` (returns executed actions; raises `AuthExpired`), `async register_rfid(tray, state, spool) -> None`, attribute `errors: list[str]` (errors of the last run).

- [ ] **Step 1: Failing tests** `tests/test_cloud_sync.py`:
```python
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
```
Run → FAIL.

- [ ] **Step 2: Implement** `cloud_sync.py`:
```python
"""Mirror Spoolman into the Bambu cloud filament library (Spoolman always wins)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

from .bambu_cloud import AuthExpired, BambuCloudError
from .const import CLOUD_WRITE_MIN_INTERVAL_S, EXTRA_CLOUD_ID, EXTRA_RFID, EXTRA_SLOT
from .model import SlotRef, cloud_field_diff, cloud_payload, extra_get, hex6

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Create:
    spool_id: int
    payload: dict


@dataclass(frozen=True)
class Update:
    spool_id: int
    cloud_id: int
    fields: dict


@dataclass(frozen=True)
class Delete:
    spool_id: int
    cloud_id: int


@dataclass(frozen=True)
class Link:
    spool_id: int
    cloud_id: int


@dataclass(frozen=True)
class Bind:
    cloud_id: int
    slot: SlotRef
    rfid: str


@dataclass(frozen=True)
class Unbind:
    slot: SlotRef


def _cloud_slot(c: dict, dev_id: str) -> SlotRef | None:
    if not c.get("inPrinter") or c.get("devId") != dev_id or c.get("amsId") is None:
        return None
    try:
        return SlotRef("ams", int(c["amsId"]), int(c.get("slotId")))
    except (TypeError, ValueError):
        return None


def plan(spools: list[dict], cloud: list[dict], dev_id: str) -> list[Any]:
    by_id = {c["id"]: c for c in cloud if isinstance(c.get("id"), int)}
    by_rfid = {str(c.get("RFID")).upper(): c for c in cloud if c.get("RFID")}
    actions: list[Any] = []
    wanted_slots: dict[int, SlotRef] = {}
    linked: set[int] = set()
    for spool in spools:
        sid = spool["id"]
        raw = extra_get(spool, EXTRA_CLOUD_ID)
        c = by_id.get(int(raw)) if raw and raw.isdigit() else None
        if spool.get("archived"):
            if c is not None:
                actions.append(Delete(sid, c["id"]))
            continue
        rfid = extra_get(spool, EXTRA_RFID)
        if c is None and rfid and (c := by_rfid.get(rfid.upper())) is not None:
            actions.append(Link(sid, c["id"]))
        if c is None:
            if not rfid:  # RFID spools are registered by the AMS path, never created as manual
                actions.append(Create(sid, cloud_payload(spool)))
            continue
        linked.add(c["id"])
        if diff := cloud_field_diff(cloud_payload(spool), c):
            actions.append(Update(sid, c["id"], diff))
        slot = SlotRef.parse(extra_get(spool, EXTRA_SLOT))
        if slot is not None and slot.kind == "ams":
            wanted_slots[c["id"]] = slot
            if _cloud_slot(c, dev_id) != slot:
                actions.append(Bind(c["id"], slot, str(c.get("RFID") or "")))
    taken = set(wanted_slots.values())
    for cid in linked:
        current = _cloud_slot(by_id[cid], dev_id)
        if current is not None and cid not in wanted_slots and current not in taken:
            actions.append(Unbind(current))
    return actions


def ams_sync_item(tray: Any, state: Any, payload: dict) -> dict:
    """`/ams/sync` item that registers an RFID spool at its slot (Studio's AmsSyncItem)."""
    color = hex6(state.color) or hex6(payload["color"]) or "FFFFFF"
    return {
        "RFID": state.rfid, "createType": "ams",
        "filamentVendor": payload["filamentVendor"], "filamentType": payload["filamentType"],
        "filamentName": payload["filamentName"], "filamentId": state.filament_id or payload["filamentId"],
        "isSupport": "support" in payload["filamentName"].lower(),
        "color": f"#{color}", "colorType": 2, "colors": [f"#{color}FF"], "rolls": 1,
        "netWeight": payload["netWeight"], "totalNetWeight": payload["totalNetWeight"], "note": "",
        "amsSn": tray.ams_serial, "slotId": str(tray.slot.slot), "amsId": tray.slot.unit,
        "amsType": tray.ams_type,
    }


class CloudSync:
    def __init__(self, hass, client, spoolman, printer, dev_id: str) -> None:
        self.hass, self.client, self.spoolman, self.printer = hass, client, spoolman, printer
        self.dev_id = dev_id
        self.errors: list[str] = []
        self._last_write: dict[int, float] = {}

    async def _cloud(self, func, *args):
        return await self.hass.async_add_executor_job(func, *args)

    def _throttled(self, cloud_id: int) -> bool:
        return time.monotonic() - self._last_write.get(cloud_id, -1e9) < CLOUD_WRITE_MIN_INTERVAL_S

    async def reconcile(self) -> list[Any]:
        """One full pass. Raises AuthExpired; other errors are collected in `errors`."""
        self.errors = []
        spools = await self.spoolman.get_spools(allow_archived=True)
        cloud = await self._cloud(self.client.get_spools)
        cloud_by_id = {c["id"]: c for c in cloud}
        done: list[Any] = []
        for action in plan(spools, cloud, self.dev_id):
            try:
                if await self._apply(action, cloud_by_id):
                    done.append(action)
            except AuthExpired:
                raise
            except BambuCloudError as err:
                self.errors.append(f"{type(action).__name__}: {err}")
        return done

    async def _apply(self, action: Any, cloud_by_id: dict[int, dict]) -> bool:
        if isinstance(action, Create):
            known = set(cloud_by_id)
            await self._cloud(self.client.create_spool, action.payload)
            fresh = [c for c in await self._cloud(self.client.get_spools)
                     if c["id"] not in known and c.get("filamentName") == action.payload["filamentName"]
                     and hex6(c.get("color")) == hex6(action.payload["color"])]
            if not fresh:
                self.errors.append(f"Create: new cloud spool for {action.spool_id} not found")
                return False
            new_id = max(c["id"] for c in fresh)
            cloud_by_id[new_id] = next(c for c in fresh if c["id"] == new_id)
            await self.spoolman.set_spool_extra(action.spool_id, {EXTRA_CLOUD_ID: str(new_id)})
            return True
        if isinstance(action, Update):
            if self._throttled(action.cloud_id):
                return False
            body = {"id": action.cloud_id,
                    "filamentName": action.fields.get("filamentName") or cloud_by_id[action.cloud_id].get("filamentName") or "",
                    **action.fields}
            if "color" in action.fields:
                body["colors"] = [action.fields["color"]]
            await self._cloud(self.client.update_spool, body)
            self._last_write[action.cloud_id] = time.monotonic()
            return True
        if isinstance(action, Delete):
            await self._cloud(self.client.delete_spools, [action.cloud_id])
            await self.spoolman.set_spool_extra(action.spool_id, {EXTRA_CLOUD_ID: None})
            return True
        if isinstance(action, Link):
            await self.spoolman.set_spool_extra(action.spool_id, {EXTRA_CLOUD_ID: str(action.cloud_id)})
            return True
        if isinstance(action, (Bind, Unbind)):
            tray = self.printer.tray_for(action.slot)
            if tray is None or not tray.ams_serial:
                self.errors.append(f"{type(action).__name__}: no AMS tray for {action.slot.key}")
                return False
            if isinstance(action, Unbind):
                state = self.printer.tray_state(tray)
                if state is None or not state.empty:
                    return False  # the cloud only accepts unbinding an empty slot
            mapping = {"amsId": action.slot.unit, "amsSn": tray.ams_serial, "amsType": tray.ams_type,
                       "rfid": action.rfid if isinstance(action, Bind) else "",
                       "slotId": str(action.slot.slot),
                       "spoolId": action.cloud_id if isinstance(action, Bind) else 0}
            results = await self._cloud(self.client.slot_mappings_sync, self.dev_id, [mapping])
            codes = [r.get("code") for r in results]
            if codes != [0]:
                self.errors.append(f"{type(action).__name__} {action.slot.key}: cloud code {codes[0] if codes else 'none'}")
                return False
            return True
        return False

    async def register_rfid(self, tray: Any, state: Any, spool: dict) -> None:
        """Create the cloud entry for an RFID spool the cloud does not know yet."""
        item = ams_sync_item(tray, state, cloud_payload(spool))
        await self._cloud(self.client.ams_sync, self.dev_id, [item])
```
Note: the error message for codes uses the phrase `code 4` — keep the string format `cloud code {n}` so the test's `"code 4"` substring matches.

- [ ] **Step 3:** `uv run pytest tests/test_cloud_sync.py -q` → PASS.
- [ ] **Step 4: Commit** `feat: cloud sync planner and executor`.

---

### Task 7: Usage ledger

**Files:**
- Create: `custom_components/spool_bridge/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: `slot_from_flat` (Task 1); constants.
- Produces: `parse_ts(value) -> float|None`; `Entry` dataclass (`job_id, ams, spool_id, grams, state, used_before, note, created`) with `key`, `to_dict()`, `from_dict()`; `select_job(tasks, dev_id, start_ts) -> dict|None`; `plan_entries(job, assignment: dict[str,int], progress: int|None, now) -> list[Entry]`; `Ledger` with `entries`, `done_jobs`, `snapshot`, `progress`, `last_end_ts`, `add_job(job, entries, now) -> bool` (False if already done), `pending() -> list[Entry]`, `prune(now)`, `to_dict()`, `from_dict()`; `async apply_entry(entry, spoolman, save) -> None`.

- [ ] **Step 1: Failing tests** `tests/test_usage.py`:
```python
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
```
Run → FAIL.

- [ ] **Step 2: Implement** `usage.py`:
```python
"""Per-job filament usage: plan deductions from cloud jobs, apply them exactly once."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .const import JOB_MATCH_TOLERANCE_S, LEDGER_TTL_DAYS, TASK_FAILED, TASK_FINISHED, USED_WEIGHT_TOLERANCE_G
from .model import slot_from_flat


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

    @property
    def key(self) -> str:
        return f"{self.job_id}:{self.ams}"

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


def plan_entries(job: dict, assignment: dict[str, int], progress: int | None, now: float) -> list[Entry]:
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
            entries.append(Entry(job["id"], ams, spool_id, round(grams * factor, 2), "pending", created=now))
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
    if entry.used_before is not None and used >= entry.used_before + entry.grams - USED_WEIGHT_TOLERANCE_G:
        entry.state = "applied"
        await save()
        return
    entry.used_before = used
    await save()
    await spoolman.use_weight(entry.spool_id, entry.grams)
    entry.state = "applied"
    await save()
```
- [ ] **Step 3:** `uv run pytest tests/test_usage.py -q` → PASS.
- [ ] **Step 4: Commit** `feat: usage ledger`.

---

### Task 8: Migration

**Files:**
- Create: `custom_components/spool_bridge/migration.py`
- Test: `tests/test_migration.py`

**Interfaces:**
- Consumes: `SpoolmanClient` methods `get_spools`, `ensure_vendor`, `ensure_filament`, `create_spool` (Task 3); `hex6`, `SlotRef`, `extra_get` (Task 1).
- Produces: `plan_migration(cloud) -> tuple[list[dict], list[dict]]` (imports, deletes); `async import_spools(spoolman, imports, dev_id, printer_name) -> int` (count created); `describe(imports, deletes) -> str` (dry-run text for the config flow).

- [ ] **Step 1: Failing tests** `tests/test_migration.py`:
```python
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
```
Run → FAIL.

- [ ] **Step 2: Implement** `migration.py`:
```python
"""One-time migration: import Bambu RFID cloud spools into Spoolman, delete manual ones."""

from __future__ import annotations

from typing import Any

from .const import EXTRA_CLOUD_ID, EXTRA_RFID, EXTRA_SLOT
from .model import SlotRef, extra_get, hex6


def plan_migration(cloud: list[dict]) -> tuple[list[dict], list[dict]]:
    imports = [c for c in cloud if c.get("createType") == "ams" and c.get("RFID")]
    deletes = [c for c in cloud if c not in imports]
    return imports, deletes


def _line(c: dict) -> str:
    return f"- {c.get('filamentVendor')} {c.get('filamentName')} #{hex6(c.get('color'))} ({c.get('netWeight')} g)"


def describe(imports: list[dict], deletes: list[dict]) -> str:
    parts = [f"Import {len(imports)} Bambu RFID spool(s) into Spoolman:", *map(_line, imports),
             "", f"Delete {len(deletes)} other spool(s) from the Bambu cloud:", *map(_line, deletes)]
    return "\n".join(parts)


async def import_spools(spoolman: Any, imports: list[dict], dev_id: str, printer_name: str) -> int:
    linked = {extra_get(s, EXTRA_CLOUD_ID) for s in await spoolman.get_spools(allow_archived=True)}
    created = 0
    for c in imports:
        if str(c["id"]) in linked:
            continue
        vendor = await spoolman.ensure_vendor(c.get("filamentVendor") or "Bambu Lab")
        total = float(c.get("totalNetWeight") or 1000)
        filament = await spoolman.ensure_filament(
            vendor, name=c.get("filamentName") or c.get("filamentType") or "PLA",
            material=c.get("filamentType") or "PLA", color_hex=hex6(c.get("color")),
            weight=total, spool_weight=250, bambu_filament_id=c.get("filamentId") or None,
        )
        slot = None
        if c.get("inPrinter") and c.get("devId") == dev_id and c.get("amsId") is not None:
            slot = SlotRef("ams", int(c["amsId"]), int(c["slotId"]))
        await spoolman.create_spool(
            filament, initial_weight=total, remaining_weight=float(c.get("netWeight") or 0),
            extra={EXTRA_CLOUD_ID: str(c["id"]), EXTRA_RFID: str(c["RFID"]).upper(),
                   EXTRA_SLOT: slot.key if slot else None},
            location=slot.label(printer_name) if slot else None,
        )
        created += 1
    return created
```
- [ ] **Step 3:** tests → PASS. **Step 4:** commit `feat: migration`.

---

### Task 9: Bridge runtime, services, sensors, diagnostics

**Files:**
- Create: `custom_components/spool_bridge/bridge.py`, `sensor.py`, `diagnostics.py`, `services.yaml`
- Modify: `custom_components/spool_bridge/__init__.py` (full setup)
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Bridge(hass, entry, spoolman, cloud_client, printer)` with `async_start()`, `async_stop()`, `async assign(spool_id, slot)`, `async unassign(slot)`, `async request_reconcile(now=False)`, `async on_tag(tag_id)`, `async on_tray_change(entity_id, old_state, new_state)`, `async on_status_change(old, new)`, `async process_finished_job(attempt=0)`, `state: dict` (`pending_jobs`, `last_sync`, `last_error`), `diagnostics() -> dict`, attribute `window: LoadWindow`, `ledger: Ledger`. `__init__.py` registers services `reconcile`, `assign {spool_id, slot}`, `unassign {slot}`.

- [ ] **Step 1: Failing tests** `tests/test_bridge.py` (uses `setup_printer`/`SERIAL` from `tests/test_printer.py`):
```python
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
```
Run → FAIL.

- [ ] **Step 2: Implement** `bridge.py`:
```python
"""Wires HA events, Spoolman, the printer and the Bambu cloud together."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .assignment import Ambiguous, Assign, Expired, LoadWindow
from .bambu_cloud import AuthExpired, BambuCloudError
from .cloud_sync import CloudSync
from .const import (
    ACTION_PREFIX, BUSY_STATES, CONF_NOTIFY_SERVICE, DOMAIN, END_STATES, EXTRA_CLOUD_ID, EXTRA_RFID,
    EXTRA_SLOT, JOB_FETCH_ATTEMPTS, JOB_FETCH_DELAY_S, LEDGER_RETRY_INTERVAL, LOAD_WINDOW_S,
    RECONCILE_DEBOUNCE_S, RECONCILE_INTERVAL, SETTLE_S, SIGNAL_STATE, STUCK_AFTER_S,
)
from .model import SlotRef, cloud_payload, extra_get, hex6, spool_filament_id, tag_to_spool_id
from .printer import Printer, parse_tray_state
from .spoolman import SpoolmanError
from .usage import Ledger, apply_entry, plan_entries, select_job

_LOGGER = logging.getLogger(__name__)


class Bridge:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, spoolman, cloud, printer: Printer) -> None:
        self.hass, self.entry, self.spoolman, self.cloud, self.printer = hass, entry, spoolman, cloud, printer
        self.dev_id = printer.serial
        self.sync = CloudSync(hass, cloud, spoolman, printer, self.dev_id)
        self.window = LoadWindow(LOAD_WINDOW_S, SETTLE_S)
        self.ledger = Ledger()
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._unsubs: list = []
        self._tasks: list[asyncio.Task] = []
        self._reconcile_handle: asyncio.TimerHandle | None = None
        self._reconcile_lock = asyncio.Lock()
        self._ledger_lock = asyncio.Lock()
        self._cloud_paused = False
        self._first_failure: float | None = None
        self._stuck_notified = False
        self.state: dict[str, Any] = {"pending_jobs": 0, "last_sync": None, "last_error": None}

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.ledger = Ledger.from_dict(stored.get("ledger") or {})
        if self.ledger.last_end_ts is None:
            self.ledger.last_end_ts = time.time()  # never book history from before install
            await self._save()
        trays = [t.entity_id for t in self.printer.trays()]
        self._unsubs += [
            self.hass.bus.async_listen("tag_scanned", self._tag_event),
            self.hass.bus.async_listen("mobile_app_notification_action", self._action_event),
            async_track_state_change_event(self.hass, trays, self._tray_event),
            async_track_time_interval(self.hass, self._interval_reconcile, RECONCILE_INTERVAL),
            async_track_time_interval(self.hass, self._interval_ledger, LEDGER_RETRY_INTERVAL),
        ]
        watched = [e for e in (self.printer.status_entity, self.printer.progress_entity) if e]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._printer_event))
        self._tasks.append(self.entry.async_create_background_task(
            self.hass, self.spoolman.listen(self._spoolman_event), f"{DOMAIN} spoolman websocket"))
        self._tasks.append(self.entry.async_create_background_task(
            self.hass, self._ticker(), f"{DOMAIN} ticker"))
        self._tasks.append(self.entry.async_create_background_task(
            self.hass, self._startup(), f"{DOMAIN} startup"))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._reconcile_handle:
            self._reconcile_handle.cancel()

    async def _startup(self) -> None:
        await self.sync_rfid_trays()
        await self.catch_up_jobs()
        await self.request_reconcile(now=True)

    async def _save(self) -> None:
        self.state["pending_jobs"] = len(self.ledger.pending())
        await self._store.async_save({"ledger": self.ledger.to_dict()})
        async_dispatcher_send(self.hass, SIGNAL_STATE)

    def _error(self, message: str) -> None:
        _LOGGER.warning(message)
        self.state["last_error"] = message
        async_dispatcher_send(self.hass, SIGNAL_STATE)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.async_tick()

    async def async_tick(self) -> None:
        now = time.time()
        decision = self.window.settle(now) or self.window.expire(now)
        if isinstance(decision, Assign):
            await self.assign(decision.spool_id, decision.slot)
        elif isinstance(decision, Ambiguous):
            await self._ask_slot(decision.spool_id, decision.slots, "More than one slot changed. Which one?")
        elif isinstance(decision, Expired):
            await self._ask_slot(decision.spool_id, tuple(t.slot for t in self.printer.trays()),
                                 "No slot change seen. Pick the slot:")

    # ------------------------------------------------------------ notifications

    async def notify(self, message: str, actions: list[dict] | None = None) -> None:
        data: dict[str, Any] = {"title": "Spool Bridge", "message": message}
        if actions:
            data["data"] = {"actions": actions[:10]}
        try:
            await self.hass.services.async_call("notify", self.entry.data[CONF_NOTIFY_SERVICE], data, blocking=False)
        except Exception as err:  # noqa: BLE001 - a missing phone must never break the bridge
            _LOGGER.warning("Notification failed: %s", err)

    async def _ask_slot(self, spool_id: int, slots: tuple[SlotRef, ...], message: str) -> None:
        name = self.printer.name()
        actions = [{"action": f"{ACTION_PREFIX}|{spool_id}|{s.key}", "title": s.label(name)} for s in slots]
        await self.notify(message, actions)

    @callback
    def _action_event(self, event: Event) -> None:
        parts = str(event.data.get("action") or "").split("|")
        if len(parts) == 3 and parts[0] == ACTION_PREFIX and parts[1].isdigit() and (slot := SlotRef.parse(parts[2])):
            self.hass.async_create_task(self.assign(int(parts[1]), slot))

    # ------------------------------------------------------------ scan -> load

    @callback
    def _tag_event(self, event: Event) -> None:
        self.hass.async_create_task(self.on_tag(event.data.get("tag_id")))

    async def on_tag(self, tag_id: str | None) -> None:
        spool_id = tag_to_spool_id(tag_id)
        if spool_id is None:
            return
        spool = await self._spool(spool_id)
        if spool is None or spool.get("archived"):
            await self.notify(f"Unknown spool {spool_id}")
            return
        self.window.scan(spool_id, time.time())
        fil = spool.get("filament") or {}
        await self.notify(f"{fil.get('name') or fil.get('material')}, {round(spool.get('remaining_weight') or 0)} g"
                          " — load it into any slot within 5 min.")

    async def _spool(self, spool_id: int) -> dict | None:
        try:
            return await self.spoolman.get_spool(spool_id)
        except SpoolmanError as err:
            self._error(f"Spoolman: {err}")
            return None

    @callback
    def _tray_event(self, event: Event) -> None:
        self.hass.async_create_task(self.on_tray_change(
            event.data["entity_id"], event.data.get("old_state"), event.data.get("new_state")))

    async def on_tray_change(self, entity_id: str, old_state, new_state) -> None:
        tray = self.printer.tray_by_entity(entity_id)
        old, new = parse_tray_state(old_state), parse_tray_state(new_state)
        if tray is None or old is None or new is None or old.empty == new.empty:
            return
        if new.empty:
            await self.unassign(tray.slot)
            return
        if new.rfid:
            await self.sync_rfid_trays()
            return
        self.window.tray_loaded(tray.slot, time.time())

    async def assign(self, spool_id: int, slot: SlotRef) -> None:
        try:
            spools = await self.spoolman.get_spools(allow_archived=False)
            spool = next((s for s in spools if s["id"] == spool_id), None)
            if spool is None:
                await self.notify(f"Unknown spool {spool_id}")
                return
            for other in spools:
                if other["id"] != spool_id and extra_get(other, EXTRA_SLOT) == slot.key:
                    await self.spoolman.set_spool_extra(other["id"], {EXTRA_SLOT: None}, location=None)
            name = self.printer.name()
            await self.spoolman.set_spool_extra(spool_id, {EXTRA_SLOT: slot.key}, location=slot.label(name))
        except SpoolmanError as err:
            self._error(f"Assign failed: {err}")
            await self.notify(f"Could not assign spool {spool_id}: Spoolman unreachable")
            return
        fil = spool.get("filament") or {}
        tray = self.printer.tray_for(slot)
        if tray is not None and not extra_get(spool, EXTRA_RFID):
            try:
                await self.printer.set_filament(tray, filament_id=spool_filament_id(spool),
                                                color_hex=fil.get("color_hex"), material=fil.get("material"),
                                                nozzle_temp=fil.get("settings_extruder_temp"))
            except Exception as err:  # noqa: BLE001
                self._error(f"set_filament {slot.key}: {err}")
        await self.request_reconcile(now=True)
        await self.notify(f"{fil.get('name') or fil.get('material')} → {slot.label(name)} ✓")

    async def unassign(self, slot: SlotRef) -> None:
        try:
            for spool in await self.spoolman.get_spools(allow_archived=False):
                if extra_get(spool, EXTRA_SLOT) == slot.key:
                    await self.spoolman.set_spool_extra(spool["id"], {EXTRA_SLOT: None}, location=None)
        except SpoolmanError as err:
            self._error(f"Unassign failed: {err}")
            return
        await self.request_reconcile(now=True)

    async def sync_rfid_trays(self) -> None:
        """Every loaded RFID tray has a Spoolman spool assigned to it (created if needed)."""
        try:
            spools = await self.spoolman.get_spools(allow_archived=False)
            for tray in self.printer.trays():
                state = self.printer.tray_state(tray)
                if state is None or state.empty or not state.rfid:
                    continue
                spool = next((s for s in spools if (extra_get(s, EXTRA_RFID) or "").upper() == state.rfid), None)
                if spool is None:
                    vendor = await self.spoolman.ensure_vendor("Bambu Lab")
                    total = float(state.tray_weight or 1000)
                    filament = await self.spoolman.ensure_filament(
                        vendor, name=state.name.removeprefix("Bambu ").strip() or state.material,
                        material=state.material or "PLA", color_hex=hex6(state.color), weight=total,
                        spool_weight=250, bambu_filament_id=state.filament_id or None)
                    remaining = total * state.remain / 100 if state.remain >= 0 else total
                    spool = await self.spoolman.create_spool(
                        filament, initial_weight=total, remaining_weight=remaining,
                        extra={EXTRA_RFID: state.rfid}, location=None)
                    spools.append(spool)
                if extra_get(spool, EXTRA_SLOT) != tray.slot.key:
                    await self.assign(spool["id"], tray.slot)
                cloud = await self.hass.async_add_executor_job(self.cloud.get_spools)
                if not any(str(c.get("RFID") or "").upper() == state.rfid for c in cloud):
                    await self.sync.register_rfid(tray, state, spool)
        except AuthExpired:
            self._auth_expired()
        except (SpoolmanError, BambuCloudError) as err:
            self._error(f"RFID sync: {err}")

    # ------------------------------------------------------------ cloud sync

    @callback
    def _spoolman_event(self, event: dict) -> None:
        if event.get("resource") in ("spool", "filament", "vendor"):
            self.hass.async_create_task(self.request_reconcile())

    async def _interval_reconcile(self, _now=None) -> None:
        await self.request_reconcile(now=True)

    async def request_reconcile(self, now: bool = False) -> None:
        if self._reconcile_handle:
            self._reconcile_handle.cancel()
        if now:
            await self._reconcile()
            return
        self._reconcile_handle = self.hass.loop.call_later(
            RECONCILE_DEBOUNCE_S, lambda: self.hass.async_create_task(self._reconcile()))

    async def _reconcile(self) -> None:
        if self._cloud_paused:
            return
        async with self._reconcile_lock:
            try:
                done = await self.sync.reconcile()
            except AuthExpired:
                self._auth_expired()
                return
            except (SpoolmanError, BambuCloudError) as err:
                self.sync.errors = [str(err)]
                done = []
            if done:
                _LOGGER.info("Cloud sync applied: %s", done)
            if self.sync.errors:
                self._error("; ".join(self.sync.errors))
                self._first_failure = self._first_failure or time.time()
                if time.time() - self._first_failure > STUCK_AFTER_S and not self._stuck_notified:
                    self._stuck_notified = True
                    await self.notify(f"Cloud sync failing for over an hour: {self.sync.errors[0]}")
            else:
                self._first_failure, self._stuck_notified = None, False
                self.state["last_sync"] = dt_util.utcnow()
                async_dispatcher_send(self.hass, SIGNAL_STATE)

    def _auth_expired(self) -> None:
        if not self._cloud_paused:
            self._cloud_paused = True
            self._error("Bambu cloud token expired - re-authentication required")
            self.entry.async_start_reauth(self.hass)

    # ------------------------------------------------------------ usage

    @callback
    def _printer_event(self, event: Event) -> None:
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if new is None:
            return
        if event.data["entity_id"] == self.printer.progress_entity:
            if self.printer.status() in BUSY_STATES:
                try:
                    self.ledger.progress = int(float(new.state))
                except ValueError:
                    return
                self.hass.async_create_task(self._save())
            return
        self.hass.async_create_task(self.on_status_change(old.state if old else None, new.state))

    async def on_status_change(self, old: str | None, new: str) -> None:
        if new in BUSY_STATES and old not in BUSY_STATES:
            assignment = await self._current_assignment()
            self.ledger.snapshot = {"start_ts": time.time(), "assignment": assignment}
            self.ledger.progress = None
            await self._save()
        elif new in END_STATES and old in BUSY_STATES:
            self._tasks.append(self.entry.async_create_background_task(
                self.hass, self._fetch_job_loop(), f"{DOMAIN} job fetch"))

    async def _current_assignment(self) -> dict[str, int]:
        try:
            spools = await self.spoolman.get_spools(allow_archived=False)
        except SpoolmanError:
            return {}
        return {slot: s["id"] for s in spools if (slot := extra_get(s, EXTRA_SLOT))}

    async def _fetch_job_loop(self) -> None:
        for attempt in range(JOB_FETCH_ATTEMPTS):
            if await self.process_finished_job():
                return
            await asyncio.sleep(JOB_FETCH_DELAY_S)
        await self.notify("A print finished but its usage never appeared in the Bambu cloud; nothing deducted.")

    async def process_finished_job(self) -> bool:
        """Book the job of the current snapshot. True when done (booked or already booked)."""
        snapshot = self.ledger.snapshot
        if snapshot is None:
            return True
        try:
            tasks = await self.hass.async_add_executor_job(self.cloud.get_tasks, 20)
        except AuthExpired:
            self._auth_expired()
            return False
        except BambuCloudError as err:
            self._error(f"Fetching print jobs: {err}")
            return False
        job = select_job(tasks, self.dev_id, snapshot["start_ts"])
        if job is None:
            return False
        async with self._ledger_lock:
            entries = plan_entries(job, snapshot["assignment"], self.ledger.progress, time.time())
            self.ledger.add_job(job, entries, time.time())
            await self._save()
        await self.apply_pending()
        for entry in entries:
            if entry.state == "skipped":
                await self.notify(f"Print usage {entry.grams} g from tray {entry.ams} not booked: {entry.note}")
        return True

    async def catch_up_jobs(self) -> None:
        """Book X2D jobs that ended while HA was down (current assignment, flagged)."""
        try:
            tasks = await self.hass.async_add_executor_job(self.cloud.get_tasks, 20)
        except AuthExpired:
            self._auth_expired()
            return
        except BambuCloudError as err:
            self._error(f"Catch-up: {err}")
            return
        from .usage import parse_ts
        since = self.ledger.last_end_ts or time.time()
        missed = [t for t in tasks if t.get("deviceId") == self.dev_id and t.get("status") in (2, 3)
                  and str(t["id"]) not in self.ledger.done_jobs and (parse_ts(t.get("endTime")) or 0) > since]
        if not missed:
            return
        assignment = await self._current_assignment()
        async with self._ledger_lock:
            for job in missed:
                self.ledger.add_job(job, plan_entries(job, assignment, None, time.time()), time.time())
            await self._save()
        await self.apply_pending()
        await self.notify(f"Booked {len(missed)} print(s) that finished while Home Assistant was down "
                          "(using the current slot assignment).")

    async def _interval_ledger(self, _now=None) -> None:
        await self.apply_pending()
        self.ledger.prune(time.time())
        await self._save()

    async def apply_pending(self) -> None:
        async with self._ledger_lock:
            for entry in self.ledger.pending():
                try:
                    await apply_entry(entry, self.spoolman, self._save)
                except Exception as err:  # noqa: BLE001 - stays pending, retried later
                    self._error(f"Usage {entry.key}: {err}")
            stuck = [e for e in self.ledger.pending() if time.time() - e.created > STUCK_AFTER_S]
            if stuck and not self._stuck_notified:
                self._stuck_notified = True
                await self.notify(f"{len(stuck)} usage deduction(s) pending for over an hour.")
            await self._save()

    # ------------------------------------------------------------ diagnostics

    def diagnostics(self) -> dict:
        return {
            "state": {k: str(v) for k, v in self.state.items()},
            "ledger": {"pending": len(self.ledger.pending()), "entries": len(self.ledger.entries),
                       "done_jobs": len(self.ledger.done_jobs), "has_snapshot": self.ledger.snapshot is not None},
            "trays": [t.slot.key for t in self.printer.trays()],
            "cloud_paused": self._cloud_paused,
        }
```
Note: `process_finished_job` returns True also when the job was already in `done_jobs` (because `add_job` returns False but nothing else happens) — the `apply_pending` call is harmless.

`sensor.py`:
```python
"""Bridge health sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_STATE

SENSORS = (("pending_jobs", "Pending usage deductions", None),
           ("last_sync", "Last cloud sync", SensorDeviceClass.TIMESTAMP),
           ("last_error", "Last error", None))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    async_add_entities(BridgeSensor(entry, key, name, dc) for key, name, dc in SENSORS)


class BridgeSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key, name, device_class) -> None:
        self._entry, self._key = entry, key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_device_class = device_class
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Spool Bridge"}

    @property
    def native_value(self):
        value = self._entry.runtime_data.state.get(self._key)
        return value[:255] if isinstance(value, str) else value

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE, self.async_write_ha_state))
```

`diagnostics.py`:
```python
"""Diagnostics (no tokens, serials, RFIDs or ids)."""

from homeassistant.components.diagnostics import async_redact_data

REDACT = {"token", "email", "printer_serial"}


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    return {"entry": async_redact_data(dict(entry.data), REDACT), "bridge": entry.runtime_data.diagnostics()}
```

`services.yaml`:
```yaml
reconcile:
  name: Reconcile
  description: Mirror Spoolman into the Bambu cloud library now.
assign:
  name: Assign spool
  description: Assign a Spoolman spool to a slot (ams:<unit>:<slot> or ext:<n>, 0-based).
  fields:
    spool_id:
      required: true
      selector: { number: { min: 1, max: 1000000, mode: box } }
    slot:
      required: true
      example: "ams:1:0"
      selector: { text: {} }
unassign:
  name: Unassign slot
  description: Clear the spool assigned to a slot.
  fields:
    slot:
      required: true
      example: "ams:1:0"
      selector: { text: {} }
```

`__init__.py`:
```python
"""Spool Bridge: Spoolman <-> Bambu X2D trays <-> Bambu cloud filament library."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bambu_cloud import BambuCloudClient
from .bridge import Bridge
from .const import CONF_PRINTER_SERIAL, CONF_REGION, CONF_SPOOLMAN_URL, CONF_TOKEN, DOMAIN
from .model import SlotRef
from .printer import Printer
from .spoolman import SpoolmanClient, SpoolmanError

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _slot(value: str) -> SlotRef:
    slot = SlotRef.parse(value)
    if slot is None:
        raise vol.Invalid("slot must look like ams:1:0 or ext:0")
    return slot


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    def bridge() -> Bridge:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            return entry.runtime_data
        raise HomeAssistantError("Spool Bridge is not set up")

    async def reconcile(call: ServiceCall) -> None:
        await bridge().request_reconcile(now=True)

    async def assign(call: ServiceCall) -> None:
        await bridge().assign(call.data["spool_id"], call.data["slot"])

    async def unassign(call: ServiceCall) -> None:
        await bridge().unassign(call.data["slot"])

    hass.services.async_register(DOMAIN, "reconcile", reconcile)
    hass.services.async_register(DOMAIN, "assign", assign, vol.Schema(
        {vol.Required("spool_id"): cv.positive_int, vol.Required("slot"): _slot}))
    hass.services.async_register(DOMAIN, "unassign", unassign, vol.Schema({vol.Required("slot"): _slot}))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    spoolman = SpoolmanClient(async_get_clientsession(hass), entry.data[CONF_SPOOLMAN_URL])
    try:
        await spoolman.ensure_extra_fields()
    except SpoolmanError as err:
        raise ConfigEntryNotReady(str(err)) from err
    cloud = BambuCloudClient(entry.data[CONF_REGION], entry.data[CONF_TOKEN])
    bridge = Bridge(hass, entry, spoolman, cloud, Printer(hass, entry.data[CONF_PRINTER_SERIAL]))
    entry.runtime_data = bridge
    await bridge.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```
The `bridge` fixture in the test does not call `async_setup_entry`, so `async_start` runs directly; Spoolman `listen` is a `MagicMock` attribute — set `sm.listen = AsyncMock()` in the fixture so the background task completes immediately.

- [ ] **Step 3:** `uv run pytest tests/test_bridge.py -q` → PASS (fix only glue issues; keep behaviour identical to the tests).
- [ ] **Step 4:** `uv run pytest -q` (all) → PASS. **Step 5:** commit `feat: bridge runtime, services, sensors, diagnostics`.

---

### Task 10: Config flow

**Files:**
- Create: `custom_components/spool_bridge/config_flow.py`, `strings.json`, `translations/en.json` (copy of strings.json)
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `SpoolmanClient.info/ensure_extra_fields/get_spools/...`, `BambuCloudClient` auth + `get_spools/delete_spools`, `plan_migration/describe/import_spools`.
- Produces: steps `user` (spoolman_url, printer_serial, notify_service) → `bambu` menu (`import_token` if a `bambu_filaments` entry exists, `login`) → `login` → optional `code`/`tfa` → `migrate` (dry-run text, `run_migration` boolean) → entry `title="Spool Bridge"`, data `{spoolman_url, printer_serial, notify_service, region, email, token}`; `reauth` → `login` → update entry. Unique id = printer serial.

- [ ] **Step 1: Failing test** `tests/test_config_flow.py`:
```python
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.test_printer import SERIAL, setup_printer


async def test_full_flow_with_token_import_and_migration(hass):
    setup_printer(hass)
    hass.services.async_register("notify", "mobile_app_phone", AsyncMock())
    MockConfigEntry(domain="bambu_filaments", data={"region": "global", "email": "x@example.com",
                                                    "token": "tok"}).add_to_hass(hass)
    sm = MagicMock()
    sm.info = AsyncMock(return_value={"version": "0.27.0"})
    sm.ensure_extra_fields = AsyncMock()
    cloud = MagicMock()
    cloud.get_spools.return_value = [
        {"id": 1, "createType": "ams", "RFID": "AB" * 16, "filamentVendor": "Bambu Lab",
         "filamentName": "PLA Matte", "color": "#FFFFFF", "netWeight": 1000},
        {"id": 2, "createType": "manual", "filamentVendor": "eSUN", "filamentName": "PLA+",
         "color": "#006633", "netWeight": 200},
    ]
    with patch("custom_components.spool_bridge.config_flow.SpoolmanClient", return_value=sm), \
         patch("custom_components.spool_bridge.config_flow.BambuCloudClient", return_value=cloud), \
         patch("custom_components.spool_bridge.config_flow.import_spools", AsyncMock(return_value=1)) as imp, \
         patch("custom_components.spool_bridge.async_setup_entry", AsyncMock(return_value=True)):
        r = await hass.config_entries.flow.async_init("spool_bridge", context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "spoolman_url": "http://sm:8000", "printer_serial": SERIAL, "notify_service": "mobile_app_phone"})
        assert r["type"] is FlowResultType.MENU
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "import_token"})
        assert r["step_id"] == "migrate"
        assert "Import 1" in r["description_placeholders"]["plan"]
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"run_migration": True})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"]["token"] == "tok" and r["data"]["printer_serial"] == SERIAL
    imp.assert_awaited_once()
    cloud.delete_spools.assert_called_once_with([2])
```
Run → FAIL.

- [ ] **Step 2: Implement** `config_flow.py`:
```python
"""Config flow: Spoolman + printer + phone, Bambu login (or token import), migration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .bambu_cloud import (
    AuthFailed, BambuCloudClient, BambuCloudError, CloudflareBlocked, CodeIncorrect,
    EmailCodeRequired, TfaRequired,
)
from .const import (
    BAMBU_FILAMENTS_DOMAIN, BAMBULAB_DOMAIN, CONF_EMAIL, CONF_NOTIFY_SERVICE, CONF_PRINTER_SERIAL,
    CONF_REGION, CONF_SPOOLMAN_URL, CONF_TOKEN, DEFAULT_SPOOLMAN_URL, DOMAIN,
)
from .migration import describe, import_spools, plan_migration
from .printer import Printer
from .spoolman import SpoolmanClient, SpoolmanError


def _select(options: list[str]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


class SpoolBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._client: BambuCloudClient | None = None
        self._tfa_key = ""
        self._cloud: list[dict] = []

    def _printers(self) -> list[str]:
        devices = dr.async_get(self.hass).devices.values()
        return sorted(ident[1] for d in devices if d.via_device_id is None
                      for ident in d.identifiers if ident[0] == BAMBULAB_DOMAIN)

    def _notify_services(self) -> list[str]:
        return sorted(s for s in self.hass.services.async_services().get("notify", {}) if s.startswith("mobile_app_"))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = SpoolmanClient(async_get_clientsession(self.hass), user_input[CONF_SPOOLMAN_URL])
            try:
                await client.info()
                await client.ensure_extra_fields()
            except SpoolmanError:
                errors["base"] = "spoolman"
            else:
                await self.async_set_unique_id(user_input[CONF_PRINTER_SERIAL])
                self._abort_if_unique_id_configured()
                self._data.update(user_input)
                return await self.async_step_bambu()
        schema = vol.Schema({
            vol.Required(CONF_SPOOLMAN_URL, default=DEFAULT_SPOOLMAN_URL): str,
            vol.Required(CONF_PRINTER_SERIAL): _select(self._printers()),
            vol.Required(CONF_NOTIFY_SERVICE): _select(self._notify_services()),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_bambu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["login"]
        if self.hass.config_entries.async_entries(BAMBU_FILAMENTS_DOMAIN):
            options.insert(0, "import_token")
        return self.async_show_menu(step_id="bambu", menu_options=options)

    async def async_step_import_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        source = self.hass.config_entries.async_entries(BAMBU_FILAMENTS_DOMAIN)[0].data
        self._data.update({CONF_REGION: source["region"], CONF_EMAIL: source["email"], CONF_TOKEN: source["token"]})
        return await self.async_step_migrate()

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_REGION] = user_input[CONF_REGION]
            self._data[CONF_EMAIL] = user_input[CONF_EMAIL].strip()
            self._client = BambuCloudClient(user_input[CONF_REGION])
            try:
                await self.hass.async_add_executor_job(self._client.login, self._data[CONF_EMAIL], user_input["password"])
            except EmailCodeRequired:
                return await self.async_step_code()
            except TfaRequired as err:
                self._tfa_key = err.tfa_key
                return await self.async_step_tfa()
            except CloudflareBlocked:
                errors["base"] = "cloudflare"
            except AuthFailed:
                errors["base"] = "invalid_auth"
            except BambuCloudError:
                errors["base"] = "cannot_connect"
            else:
                return await self._logged_in()
        schema = vol.Schema({
            vol.Required(CONF_REGION, default=self._data.get(CONF_REGION, "global")): _select(["global", "china"]),
            vol.Required(CONF_EMAIL, default=self._data.get(CONF_EMAIL, "")): str,
            vol.Required("password"): str,
        })
        return self.async_show_form(step_id="login", data_schema=schema, errors=errors)

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._client.login_with_code, self._data[CONF_EMAIL], user_input["code"].strip())
            except CodeIncorrect:
                errors["base"] = "code_incorrect"
            except BambuCloudError:
                errors["base"] = "cannot_connect"
            else:
                return await self._logged_in()
        return self.async_show_form(step_id="code", data_schema=vol.Schema({vol.Required("code"): str}),
                                    errors=errors, description_placeholders={"email": self._data[CONF_EMAIL]})

    async def async_step_tfa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._client.login_with_tfa, self._tfa_key, user_input["code"].strip())
            except CodeIncorrect:
                errors["base"] = "code_incorrect"
            except BambuCloudError:
                errors["base"] = "cannot_connect"
            else:
                return await self._logged_in()
        return self.async_show_form(step_id="tfa", data_schema=vol.Schema({vol.Required("code"): str}), errors=errors)

    async def _logged_in(self) -> ConfigFlowResult:
        self._data[CONF_TOKEN] = self._client.token
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data={**entry.data, CONF_TOKEN: self._client.token})
        return await self.async_step_migrate()

    async def async_step_migrate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        client = BambuCloudClient(self._data[CONF_REGION], self._data[CONF_TOKEN])
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                self._cloud = await self.hass.async_add_executor_job(client.get_spools)
            except BambuCloudError:
                return self.async_abort(reason="cannot_connect")
        else:
            if user_input["run_migration"]:
                imports, deletes = plan_migration(self._cloud)
                spoolman = SpoolmanClient(async_get_clientsession(self.hass), self._data[CONF_SPOOLMAN_URL])
                try:
                    await import_spools(spoolman, imports, self._data[CONF_PRINTER_SERIAL],
                                        Printer(self.hass, self._data[CONF_PRINTER_SERIAL]).name())
                    if deletes:
                        await self.hass.async_add_executor_job(client.delete_spools, [c["id"] for c in deletes])
                except (SpoolmanError, BambuCloudError):
                    errors["base"] = "migration_failed"
            if not errors:
                return self.async_create_entry(title="Spool Bridge", data=self._data)
        imports, deletes = plan_migration(self._cloud)
        return self.async_show_form(
            step_id="migrate", data_schema=vol.Schema({vol.Required("run_migration", default=False): bool}),
            errors=errors, description_placeholders={"plan": describe(imports, deletes)})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._data = dict(entry_data)
        return await self.async_step_login()
```

`strings.json` (also copied to `translations/en.json`):
```json
{
  "config": {
    "step": {
      "user": {"title": "Spool Bridge", "data": {"spoolman_url": "Spoolman URL", "printer_serial": "Printer", "notify_service": "Phone (notify service)"}},
      "bambu": {"title": "Bambu cloud account", "menu_options": {"import_token": "Reuse the Bambu Filaments login", "login": "Sign in"}},
      "login": {"title": "Sign in to Bambu", "data": {"region": "Region", "email": "Email", "password": "Password"}},
      "code": {"title": "Verification code", "description": "Enter the code Bambu sent to {email}.", "data": {"code": "Code"}},
      "tfa": {"title": "Two-factor code", "data": {"code": "Code"}},
      "migrate": {"title": "One-time migration", "description": "{plan}\n\nDeleting cloud spools cannot be undone.", "data": {"run_migration": "Run this migration now"}}
    },
    "error": {"spoolman": "Spoolman is not reachable at this URL.", "cloudflare": "Blocked by Bambu's bot protection, try again later.", "invalid_auth": "Wrong email or password.", "cannot_connect": "Cannot reach the Bambu cloud.", "code_incorrect": "Code rejected.", "migration_failed": "Migration failed; nothing was deleted if the import failed."},
    "abort": {"already_configured": "This printer is already set up.", "cannot_connect": "Cannot reach the Bambu cloud.", "reauth_successful": "Signed in again."}
  }
}
```
- [ ] **Step 3:** `uv run pytest -q` → PASS. **Step 4:** commit `feat: config flow with token import and migration`.

---

### Task 11: CI, README, release metadata

**Files:**
- Create: `.github/workflows/ci.yml`, `README.md`, `NOTICE`

- [ ] **Step 1:** `.github/workflows/ci.yml`:
```yaml
name: CI
on: [push, pull_request]
jobs:
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: home-assistant/actions/hassfest@master
  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hacs/action@main
        with: { category: integration, ignore: brands }
  tests:
    runs-on: ubuntu-latest
    services:
      spoolman:
        image: ghcr.io/donkie/spoolman:0.27.0
        ports: ["7912:8000"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - run: uv run pytest -q
        env: { SPOOLMAN_URL: "http://localhost:7912" }
```
- [ ] **Step 2:** `README.md`: what it does, requirements (HA ≥ 2026.8, ha-bambulab, Spoolman add-on, HA iOS app ≥ 2026.5.0), install via HACS custom repo, setup steps, labels (set Spoolman *Settings → General → Base URL* to `https://www.home-assistant.io/tag`, print QR labels with "Use HTTP URL"; or write NFC tags in the HA app with tag id `spool-<id>`), the scan → load flow, the "edit in Spoolman only" rule, services, limitations (X2D only, external holders not bound in the cloud), credits (ha-bambu-filaments MIT, open-bamboo-networking research, Studio source). `NOTICE`: attribution text for the reused MIT code including its copyright line from the original LICENSE.
- [ ] **Step 3:** `uv run pytest -q` → PASS; grep the tree for the real serials, RFIDs and email → no matches. Commit `chore: CI, README, notice`.

---

### Task 12: Publish and deploy

No code; every step is verified by observing the live system.

- [ ] **Step 1: Publish.** `gh repo create d1ggs/ha-spool-bridge --public --source . --push`; `git tag v0.1.0 && git push --tags`; `gh release create v0.1.0 --generate-notes`. Wait for CI green: `gh run watch`.
- [ ] **Step 2: Spoolman add-on.** `ssh ha 'ha store repositories add https://github.com/fabio-garavini/hassio-addons'`; find the slug (`ha store addons | grep -i spoolman`), `ha apps install <slug>` (or `ha addons install`), `ha addons start <slug>`; read the internal hostname from `ha addons info <slug>` (`hostname:`), confirm `curl` from HA core reaches `http://<hostname>:8000/api/v1/info` (via `ssh ha 'curl -s …'`).
- [ ] **Step 3: Install the bridge.** HACS websocket: `hacs/repositories/add` `{repository: "d1ggs/ha-spool-bridge", category: "integration"}`, then `hacs/repository/download` with `version: "v0.1.0"`. Verify files on HA match the tag (hash compare as done for bambu_filaments). `ha core check`, `ha core restart`.
- [ ] **Step 4: Configure.** Drive the config flow over REST (`POST /api/config/config_entries/flow` `{handler: "spool_bridge"}` then `POST /api/config/config_entries/flow/<id>` per step): Spoolman URL from Step 2, printer = X2D serial, notify = the phone's `mobile_app_*` service, menu `import_token`, read the migrate dry-run text, check it lists 8 imports + 5 deletes, submit `run_migration: true` (user explicitly asked: import Bambu, delete the others).
- [ ] **Step 5: Retire ha-bambu-filaments.** Delete its config entry (`DELETE /api/config/config_entries/entry/<id>`), remove it in HACS (`hacs/repository/remove`), delete `/config/custom_components/bambu_filaments` if HACS left it, restart HA.
- [ ] **Step 6: Spoolman labels.** `POST {spoolman}/api/v1/setting/base_url` body `"\"https://www.home-assistant.io/tag\""`.
- [ ] **Step 7: Verify live (automatable part).** Spoolman lists 8 imported spools with `bambu_cloud_id`; cloud library has exactly those 8 and no manual spools; sensors `last_sync` set and `last_error` empty; logs free of errors (`ha core logs | grep spool_bridge`); the loaded RFID trays (AMS 1 slots 3–4, AMS 2 slot 4) are assigned in Spoolman and bound in the cloud.
- [ ] **Step 8: Hand the physical checklist to the user** (spec §11 items 1–5): scan a third-party spool and load it; check Studio profile + Filament Manager; print; check grams; cancel a print; unload; check the cloud unbind.
