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


async def test_printer_list_uses_no_deprecated_registry_api(hass, caplog):
    from custom_components.spool_bridge.config_flow import SpoolBridgeConfigFlow
    setup_printer(hass)
    flow = SpoolBridgeConfigFlow()
    flow.hass = hass
    assert flow._printers() == [SERIAL]
    assert "deprecated" not in caplog.text


async def test_options_flow_validates_and_saves_name_rules(hass):
    entry = MockConfigEntry(domain="spool_bridge", data={"printer_serial": SERIAL})
    entry.add_to_hass(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "init"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"filament_name_rules": "(unclosed"})
    assert r["errors"] == {"filament_name_rules": "invalid_rules"}
    assert "line 1" in r["description_placeholders"]["error"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"filament_name_rules": "™"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"filament_name_rules": "™"}


async def test_options_flow_checks_and_saves_bambuddy(hass):
    entry = MockConfigEntry(domain="spool_bridge", data={"printer_serial": SERIAL})
    entry.add_to_hass(hass)
    from custom_components.spool_bridge.bambuddy import BambuddyError
    with patch("custom_components.spool_bridge.config_flow.BambuddyClient") as client_cls:
        client_cls.return_value.printer_id = AsyncMock(side_effect=BambuddyError("HTTP 401: bad key"))
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {
            "bambuddy_url": "http://127.0.0.1:8000", "bambuddy_api_key": " bad "})
        assert r["errors"] == {"base": "bambuddy"}
        assert "401" in r["description_placeholders"]["error"]
        client_cls.return_value.printer_id = AsyncMock(return_value=1)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {
            "bambuddy_url": "http://127.0.0.1:8000", "bambuddy_api_key": " good "})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"filament_name_rules": "", "bambuddy_url": "http://127.0.0.1:8000",
                             "bambuddy_api_key": "good"}
    assert client_cls.call_args.args[1:] == ("http://127.0.0.1:8000", "good")
