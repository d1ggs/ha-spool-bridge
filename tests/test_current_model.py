import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from homeassistant.helpers import entity_registry as er

from custom_components.spool_bridge.printer import ModelInfo, Printer, read_3mf_model
from custom_components.spool_bridge.sensor import CurrentModelSensor
from tests.test_printer import SERIAL, setup_printer

# ha-bambulab caches each print's 3MF as "<size>-<name>.gcode.3mf" and reports the
# extracted G-code "<size>-<name>.gcode.gcode" on its gcode_file_downloaded sensor.
CACHED = "1839412-Fake_Ship,_v2.gcode"


def make_3mf(path: Path, title: str | None = "Fake Ship, v2", designer: str | None = "Someone",
             plates: dict[int, str] | None = None, slice_index: int = 3) -> Path:
    plates = {3: "boat hull #1", 16: "cabin_merry"} if plates is None else plates
    meta = "".join(f'<metadata name="{k}">{v}</metadata>'
                   for k, v in (("Title", title), ("Designer", designer)) if v is not None)
    model = f'<?xml version="1.0"?><model unit="millimeter">{meta}<resources/></model>'
    settings = "<config>" + "".join(
        f'<plate><metadata key="plater_id" value="{n}"/><metadata key="plater_name" value="{name}"/></plate>'
        for n, name in plates.items()) + "</config>"
    slice_info = f'<config><plate><metadata key="index" value="{slice_index}"/></plate></config>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", settings)
        z.writestr("Metadata/slice_info.config", slice_info)
    return path


def test_read_3mf_model_title_designer_and_plate(tmp_path):
    info = read_3mf_model(make_3mf(tmp_path / "a.3mf"), 16)
    assert info == ModelInfo(title="Fake Ship, v2", designer="Someone", plate=16,
                             plate_name="cabin_merry", file="a.3mf")


def test_read_3mf_model_unescapes_and_falls_back_to_slice_plate(tmp_path):
    # No plate from the printer: the plate the 3MF was sliced for is the one printed.
    info = read_3mf_model(make_3mf(tmp_path / "a.3mf", title="Tom &amp; Jerry"), None)
    assert info.title == "Tom & Jerry"
    assert (info.plate, info.plate_name) == (3, "boat hull #1")


def test_read_3mf_model_unnamed_plate_and_missing_title(tmp_path):
    info = read_3mf_model(make_3mf(tmp_path / "a.3mf", title=None, designer=None, plates={16: ""}), 16)
    assert info.title is None and info.designer is None
    assert (info.plate, info.plate_name) == (16, None)


def test_read_3mf_model_not_a_zip(tmp_path):
    bad = tmp_path / "a.3mf"
    bad.write_bytes(b"not a zip")
    assert read_3mf_model(bad, 16) is None


def setup_model_sensors(hass, downloaded: str, gcode_file: str) -> None:
    printer_dev = er.async_get(hass).async_get("sensor.status").device_id
    ents = er.async_get(hass)
    for key, value in (("gcode_file_downloaded", downloaded), ("gcode_file", gcode_file)):
        ent = ents.async_get_or_create("sensor", "bambu_lab", f"{SERIAL}_{key}",
                                       device_id=printer_dev, suggested_object_id=key)
        hass.states.async_set(ent.entity_id, value)


async def test_printer_current_model_reads_the_cached_3mf(hass):
    setup_printer(hass)
    make_3mf(Path(hass.config.path("www/media/ha-bambulab", SERIAL, "prints", "cache", CACHED + ".3mf")))
    setup_model_sensors(hass, CACHED + ".gcode", "/data/Metadata/plate_16.gcode")
    info = await Printer(hass, SERIAL).current_model()
    assert (info.title, info.plate, info.plate_name) == ("Fake Ship, v2", 16, "cabin_merry")


async def test_printer_current_model_none_without_a_download(hass):
    setup_printer(hass)
    for downloaded in ("unknown", "ERROR", "missing-file.gcode.gcode"):
        setup_model_sensors(hass, downloaded, "/data/Metadata/plate_16.gcode")
        assert await Printer(hass, SERIAL).current_model() is None


async def test_current_model_sensor_follows_the_download(hass):
    setup_printer(hass)
    make_3mf(Path(hass.config.path("www/media/ha-bambulab", SERIAL, "prints", "cache", CACHED + ".3mf")))
    setup_model_sensors(hass, "unknown", "unknown")
    entry = MagicMock(entry_id="e1")
    entry.runtime_data.printer = Printer(hass, SERIAL)
    sensor = CurrentModelSensor(entry)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()
    await sensor.async_added_to_hass()
    assert sensor.native_value is None
    # A new print: ha-bambulab finishes the download, then the printer names the plate.
    hass.states.async_set("sensor.gcode_file_downloaded", CACHED + ".gcode")
    hass.states.async_set("sensor.gcode_file", "/data/Metadata/plate_16.gcode")
    await hass.async_block_till_done()
    assert sensor.native_value == "Fake Ship, v2"
    assert sensor.extra_state_attributes == {
        "designer": "Someone", "plate": 16, "plate_name": "cabin_merry", "file": CACHED + ".3mf"}
