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
                                       name=f"X2D_{SERIAL}_AMS_{n}", model="AMS 2 Pro", via_device_id=printer.id)
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
    enc = ents.async_get_or_create("binary_sensor", "bambu_lab", f"{SERIAL}_mqtt_encryption",
                                   device_id=printer.id, suggested_object_id="mqtt_encryption")
    pw = ents.async_get_or_create("sensor", "bambu_lab", f"{SERIAL}_print_weight", device_id=printer.id,
                                  suggested_object_id="print_weight")
    hass.states.async_set(st.entity_id, "running")
    hass.states.async_set(pr.entity_id, "37")
    hass.states.async_set(enc.entity_id, "off")
    hass.states.async_set(pw.entity_id, "0", {})


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


def test_print_weights_maps_trays_and_external(hass):
    setup_printer(hass)
    # ha-bambulab's per-tray grams: "AMS <u> Tray <t>" (1-based) and "External Spool".
    hass.states.async_set("sensor.print_weight", "34.5",
                          {"AMS 1 Tray 1": 19.5, "AMS 2 Tray 2": 11.9, "External Spool": 3.1})
    assert Printer(hass, SERIAL).print_weights() == {0: 19.5, 5: 11.9, 254: 3.1}


def test_print_weights_empty_when_sensor_blank(hass):
    setup_printer(hass)
    assert Printer(hass, SERIAL).print_weights() == {}


def test_control_blocked_tracks_encryption_sensor(hass):
    setup_printer(hass)
    p = Printer(hass, SERIAL)
    assert p.control_blocked() is False
    hass.states.async_set("binary_sensor.mqtt_encryption", "on")
    assert p.control_blocked() is True


def test_control_not_blocked_when_sensor_missing(hass):
    setup_printer(hass)
    er.async_get(hass).async_remove("binary_sensor.mqtt_encryption")
    assert Printer(hass, SERIAL).control_blocked() is False


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
        hass.states.async_set(call.data["entity_id"], "Generic PLA", {"filament_id": call.data["tray_info_idx"]})

    hass.services.async_register("bambu_lab", "set_filament", handler)
    p = Printer(hass, SERIAL)
    tray = p.tray_for(SlotRef("ams", 0, 0))
    await p.set_filament(tray, filament_id="GFL03", color_hex="006633", material="PLA", nozzle_temp=215)
    assert calls[0].data == {"entity_id": tray.entity_id, "tray_info_idx": "GFL03", "tray_color": "006633FF",
                             "tray_type": "PLA", "nozzle_temp_min": 205, "nozzle_temp_max": 225}


async def test_name_uses_no_deprecated_registry_api(hass, caplog):
    setup_printer(hass)
    assert Printer(hass, SERIAL).name() == "X2D"
    assert "deprecated" not in caplog.text
