from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.spool_bridge as integration
from tests.test_printer import SERIAL

DATA = {"spoolman_url": "http://sm:8000", "printer_serial": SERIAL, "notify_service": "mobile_app_x",
        "region": "global", "email": "x@example.com", "token": "t"}


async def setup(hass, options):
    entry = MockConfigEntry(domain="spool_bridge", data=DATA, options=options)
    entry.add_to_hass(hass)
    with patch.object(integration, "Bridge") as bridge_cls, \
            patch.object(integration.SpoolmanClient, "ensure_extra_fields", AsyncMock()) as fields, \
            patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        bridge_cls.return_value.async_start = AsyncMock()
        assert await integration.async_setup_entry(hass, entry)
    return bridge_cls.call_args.kwargs, fields.call_args.args


async def test_without_bambuddy_no_gateway(hass):
    kwargs, fields = await setup(hass, {"filament_name_rules": ""})
    assert kwargs["bambuddy"] is None and kwargs["proxy"] is None
    assert fields == ((),)


async def test_bambuddy_key_starts_gateway_on_loopback(hass):
    kwargs, fields = await setup(hass, {"bambuddy_url": "http://127.0.0.1:8000", "bambuddy_api_key": "k"})
    assert kwargs["bambuddy"] is not None
    assert kwargs["proxy"].url == "http://127.0.0.1:7913"
    assert {key for _, key, _ in fields[0]} == {"tag", "bambu_slicer_filament"}
