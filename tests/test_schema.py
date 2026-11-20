import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from wb.mqtt_dmx import schema

ROOT = Path(__file__).parent.parent
SCHEMA_FILE = ROOT / "wb-mqtt-dmx.schema.json"
CONFIG_FILE = ROOT / "wb-mqtt-dmx.conf"


@pytest.fixture(name="form")
def form_fixture() -> dict[str, Any]:
    return schema.build_schema()


def port_of(form: dict[str, Any]) -> dict[str, Any]:
    return form["properties"]["ports"]["items"]["properties"]


def test_shipped_schema_file_matches_the_module() -> None:
    assert (
        SCHEMA_FILE.read_text(encoding="utf-8") == schema.schema_text()
    ), "the form changed: run python3 -m wb.mqtt_dmx.schema > wb-mqtt-dmx.schema.json"


def test_schema_is_a_valid_draft_4_schema(form: dict[str, Any]) -> None:
    jsonschema.Draft4Validator.check_schema(form)


def test_shipped_config_is_valid_against_the_schema(form: dict[str, Any]) -> None:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    jsonschema.validate(config, form, cls=jsonschema.Draft4Validator)


def test_schema_points_confed_at_the_config_and_the_service(form: dict[str, Any]) -> None:
    assert form["configFile"] == {"path": "/etc/wb-mqtt-dmx.conf", "service": "wb-mqtt-dmx"}


def test_ports_are_tabs_and_devices_are_a_table(form: dict[str, Any]) -> None:
    assert form["properties"]["ports"]["_format"] == "tabs-top"
    assert port_of(form)["devices"]["_format"] == "table"


def test_enabled_checkbox_sits_above_the_collapsed_settings(form: dict[str, Any]) -> None:
    port = port_of(form)

    assert port["enabled"]["propertyOrder"] == 0 and port["enabled"]["default"] is False
    assert "enabled" not in port["settings"]["properties"]
    assert port["settings"]["title"] == "Port settings" and "headerTemplate" not in port["settings"]
    assert port["settings"]["options"]["collapsed"] is True


def test_network_input_is_always_open_and_timing_is_folded_away(form: dict[str, Any]) -> None:
    settings = port_of(form)["settings"]["properties"]

    assert settings["network"]["options"].get("disable_collapse") is True
    assert "collapsed" not in settings["network"]["options"]
    assert settings["advanced"]["options"]["collapsed"] is True


def test_every_port_setting_has_a_caption_in_both_languages(form: dict[str, Any]) -> None:
    russian = form["translations"]["ru"]
    settings = port_of(form)["settings"]["properties"]
    fields = {**settings, **settings["network"]["properties"], **settings["advanced"]["properties"]}
    fields["enabled"] = port_of(form)["enabled"]

    for key, field in fields.items():
        assert field.get("description"), f"{key} has no caption"
        assert russian.get(field["description"]), f"{key}: the caption is not translated"


@pytest.mark.parametrize(
    "key", ["refresh_hz", "on_start", "on_stop", "slots", "break_us", "mab_us", "slot_gap_us"]
)
def test_caption_names_the_default(form: dict[str, Any], key: str) -> None:
    settings = port_of(form)["settings"]["properties"]
    field = {**settings, **settings["advanced"]["properties"]}[key]

    assert "Default" in field["description"]
    assert "По умолчанию" in form["translations"]["ru"][field["description"]]


def test_nothing_hides_or_requires_a_port_setting(form: dict[str, Any]) -> None:
    """
    The form shows only the settings the saved config has, and confed refuses to open a config that
    lacks a required one. So a setting must be neither hidden by a dependency nor required.
    """
    settings = port_of(form)["settings"]
    blocks = [settings, settings["properties"]["network"], settings["properties"]["advanced"]]

    assert settings["required"] == ["path"]
    assert all("required" not in block for block in blocks[1:])
    for block in blocks:
        for key, field in block["properties"].items():
            assert "dependencies" not in field.get("options", {}), key


def test_shipped_config_has_every_setting_of_the_form(form: dict[str, Any]) -> None:
    settings = port_of(form)["settings"]["properties"]
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    for port in config["ports"]:
        assert sorted(port) == ["devices", "enabled", "settings"]
        assert sorted(port["settings"]) == sorted(settings)
        assert sorted(port["settings"]["network"]) == sorted(settings["network"]["properties"])
        assert sorted(port["settings"]["advanced"]) == sorted(settings["advanced"]["properties"])


def test_device_fields_follow_the_device_type(form: dict[str, Any]) -> None:
    device = port_of(form)["devices"]["items"]["properties"]

    assert device["type"]["enum"] == ["layout", "channels"]
    assert device["type"]["options"]["enum_titles"] == ["Layout", "Channels"]
    assert device["layout"]["options"]["dependencies"] == {"type": ["layout"]}
    assert device["bits"]["options"]["dependencies"] == {"type": ["layout"]}
    assert device["channels"]["options"]["dependencies"] == {"type": ["channels"]}
    assert device["layout"]["default"] == "rgbw" and device["bits"]["enum"] == [8, 16]


def test_identical_device_rows_are_refused_by_the_form(form: dict[str, Any]) -> None:
    row = {"name": "A", "type": "layout", "layout": "rgbw"}
    config = {"ports": [{"settings": {"path": "/dev/ttyRS485-2"}, "devices": [row, dict(row)]}]}

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(config, form, cls=jsonschema.Draft4Validator)


def test_every_title_and_enum_title_is_translated(form: dict[str, Any]) -> None:
    russian = form["translations"]["ru"]
    names = {"Art-Net", "sACN"}  # proper names, the same in both languages
    missing: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            texts = [node.get("title"), *node.get("options", {}).get("enum_titles", [])]
            missing.extend(t for t in texts if isinstance(t, str) and t not in russian and t not in names)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk({key: value for key, value in form.items() if key != "translations"})

    assert not missing
