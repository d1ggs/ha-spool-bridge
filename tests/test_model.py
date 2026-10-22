from custom_components.spool_bridge.model import (
    SlotRef, bambuddy_slot, cloud_field_diff, cloud_payload, default_filament_id, extra_get,
    extra_value, hex6, slot_from_bambuddy, slot_from_flat, spool_filament_id, tag_to_spool_id, tray_color,
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


def test_spool_preset_from_bambuddy_wins_when_it_is_a_printer_profile():
    # Bambuddy keeps a per-spool preset in spool.extra.bambu_slicer_filament, as a
    # filament id ("GFA01"), a setting id ("GFSA01_07") or something the printer
    # cannot take directly (cloud user preset, local preset number, Orca UUID).
    fil = {"material": "PLA", "extra": {"bambu_filament_id": '"GFL03"'}}
    def with_preset(value):
        return {"filament": fil, "extra": {"bambu_slicer_filament": f'"{value}"'}}
    assert spool_filament_id(with_preset("GFA01")) == "GFA01"
    assert spool_filament_id(with_preset("GFSA01_07")) == "GFA01"
    assert spool_filament_id(with_preset("PMPL07")) == "PMPL07"
    for unusable in ("PFUS990b6e19965353", "PFCN12ab", "38", "0f8e2c1a-1b2c-4d5e-8f90-123456789abc"):
        assert spool_filament_id(with_preset(unusable)) == "GFL03"


def test_bambuddy_slot_mapping():
    # Bambuddy numbers AMS units 0.., and both external holders as AMS 255
    # (tray 0 = left/254, tray 1 = right/255); AMS HT (128+) is out of scope.
    assert bambuddy_slot(SlotRef("ams", 1, 2)) == (1, 2)
    assert bambuddy_slot(SlotRef("ext", 1)) == (255, 1)
    assert slot_from_bambuddy(1, 2) == SlotRef("ams", 1, 2)
    assert slot_from_bambuddy(255, 0) == SlotRef("ext", 0)
    assert slot_from_bambuddy(128, 0) is None
    assert slot_from_bambuddy(255, 2) is None


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
