"""Anonymise raw Bambu cloud captures into test fixtures.

usage: python scripts/anonymize_fixtures.py spools|tasks RAW.json OUT.json
"""
import json
import sys

SPOOL_KEYS = ["id", "createType", "filamentVendor", "filamentType", "filamentName", "filamentId",
              "RFID", "color", "colors", "netWeight", "totalNetWeight", "inPrinter", "devId",
              "amsSn", "amsId", "slotId", "amsType", "deviceName", "note", "displayName"]
TASK_KEYS = ["id", "deviceId", "deviceModel", "status", "startTime", "endTime", "weight", "amsDetailMapping"]
SERIAL_KEYS = {"devId", "deviceId", "amsSn"}


class Faker:
    """Sequential placeholders: nothing about the real value can be recovered."""

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def __call__(self, value: str, length: int, prefix: str = "") -> str:
        if not value:
            return value
        if value not in self.seen:
            n = len(self.seen) + 1
            self.seen[value] = (prefix + str(n).rjust(length, "0"))[-length:] if not prefix else (prefix + str(n).rjust(length - len(prefix), "0"))
        return self.seen[value]


def main() -> None:
    kind, raw_path, out_path = sys.argv[1:4]
    raw = json.load(open(raw_path))
    keys = SPOOL_KEYS if kind == "spools" else TASK_KEYS
    ids: dict[int, int] = {}
    fake = Faker()
    out = []
    for hit in raw["hits"]:
        item = {k: hit.get(k) for k in keys if k in hit}
        item["id"] = ids.setdefault(hit["id"], 1000 + len(ids))
        for k in SERIAL_KEYS & item.keys():
            if item[k]:
                item[k] = fake(item[k], len(item[k]), "FAKE")
        if item.get("RFID"):
            item["RFID"] = fake(item["RFID"], 32, "F")
        out.append(item)
    json.dump({"hits": out}, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
