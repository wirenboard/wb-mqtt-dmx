import json
from pathlib import Path

import pytest

from wb.mqtt_dmx import schema, templates

ROOT = Path(__file__).parent.parent
TEMPLATES = templates.load_templates([ROOT / "templates"])


def test_base_templates_load_with_expected_footprints():
    assert {t: TEMPLATES[t].footprint for t in TEMPLATES} == {
        "dimmer": 1, "rgb": 3, "grb": 3, "rgbw": 4, "grbw": 4, "drgb": 4, "drgbw": 5, "cct": 2, "rgbcct": 5}
    assert [c.id for c in TEMPLATES["grbw"].channels] == ["green", "red", "blue", "white"]
    assert TEMPLATES["rgbw"].title["ru"].startswith("RGBW")


def test_template_offsets_and_errors():
    t = templates.parse_template({"type": "x", "channels": [{"id": "a"}, {"id": "b", "offset": 5}, {"id": "c"}]})
    assert [(c.id, c.offset) for c in t.channels] == [("a", 0), ("b", 5), ("c", 6)] and t.footprint == 7
    with pytest.raises(templates.TemplateError, match="duplicate id"):
        templates.parse_template({"type": "x", "channels": [{"id": "a"}, {"id": "a"}]})
    with pytest.raises(templates.TemplateError, match="type must match"):
        templates.parse_template({"type": "Bad Type", "channels": [{"id": "a"}]})
    with pytest.raises(templates.TemplateError, match="non-empty"):
        templates.parse_template({"type": "x", "channels": []})


def test_user_directory_overrides_base(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "rgbw.json").write_text(json.dumps({"type": "rgbw", "channels": [{"id": "w"}]}), encoding="utf-8")
    loaded = templates.load_templates([ROOT / "templates", user])
    assert loaded["rgbw"].footprint == 1 and loaded["rgb"].footprint == 3


def test_shipped_config_is_valid_for_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    s = schema.build_schema(TEMPLATES)
    shipped = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))
    jsonschema.validate(shipped, s)
    def with_devices(devices):
        return {"ports": [{**shipped["ports"][0], "devices": devices}]}
    jsonschema.validate(with_devices([{"id": "facade", "name": "Фасад", "type": "rgbw", "address": 0, "count": 40}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_devices([{"id": "Bad", "type": "rgbw"}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_devices([{"id": "x", "type": "nope"}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"ports": []}, s)


def test_schema_lists_template_types_with_titles_and_translations():
    s = schema.build_schema(TEMPLATES)
    port = s["properties"]["ports"]["items"]
    dev = port["properties"]["devices"]["items"]["properties"]["type"]
    assert dev["enum"] == ["dimmer", "rgb", "grb", "rgbw", "grbw", "drgb", "drgbw", "cct", "rgbcct"]
    assert dev["options"]["enum_titles"][dev["enum"].index("rgbw")] == "RGBW (4 ch)"
    assert s["translations"]["ru"]["RGBW (4 ch)"] == "RGBW (4 канала)"
    assert s["configFile"] == {"path": "/etc/wb-mqtt-dmx.conf", "service": "wb-mqtt-dmx"}
    adv = port["properties"]["settings"]["properties"]["advanced"]
    assert port["title"] == "Port" and s["translations"]["ru"]["Ports"] == "Порты"
    assert s["properties"]["ports"]["_format"] == "tabs-top" and port["properties"]["devices"]["_format"] == "table"
    settings = port["properties"]["settings"]["properties"]
    assert settings["enabled"]["propertyOrder"] == 0 and settings["enabled"]["default"] is False
    shipped = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))
    assert [p["settings"]["path"] for p in shipped["ports"]] == ["/dev/ttyRS485-1", "/dev/ttyRS485-2"]
    assert not any(p["settings"]["enabled"] for p in shipped["ports"])
    assert adv["options"]["collapsed"] is True and "start_scene" in adv["properties"]


def test_write_schema_skips_unchanged(tmp_path):
    s = schema.build_schema(TEMPLATES)
    p = tmp_path / "s.json"
    assert schema.write_schema(s, p) is True
    assert schema.write_schema(s, p) is False
