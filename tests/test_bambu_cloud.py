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
