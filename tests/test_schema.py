import json
from pathlib import Path

import pytest

from wb.mqtt_dmx import schema, templates

ROOT = Path(__file__).parent.parent
TEMPLATES = templates.load_templates([ROOT / "templates"])


def test_layout_strings_become_templates():
    t = templates.layout_template("dRGBW")
    assert [(c.id, c.offset) for c in t.channels] == [("dimmer", 0), ("red", 1), ("green", 2), ("blue", 3), ("white", 4)]
    assert t.footprint == 5 and t.title["ru"] == "Раскладка"
    t16 = templates.layout_template("ch", 16)
    assert [(c.id, c.offset, c.bits) for c in t16.channels] == [("cold", 0, 16), ("warm", 2, 16)] and t16.footprint == 4
    skip = templates.layout_template("r_g_b")
    assert [c.offset for c in skip.channels] == [0, 2, 4] and skip.footprint == 5
    tail = templates.layout_template("drgb___")        # unused channels at the end still take addresses
    assert [c.id for c in tail.channels] == ["dimmer", "red", "green", "blue"] and tail.footprint == 7
    assert templates.layout_template("d_", 16).footprint == 4
    par = templates.layout_template("drgbwms")           # a 7-channel PAR: dimmer, colour, white, mode, strobe
    assert [c.id for c in par.channels][-2:] == ["mode", "strobe"] and par.channels[5].title["ru"] == "Режим"
    twice = templates.layout_template("ww")
    assert [c.id for c in twice.channels] == ["white", "white_2"] and twice.channels[1].title["ru"] == "Белый 2"
    for bad in ("", "xyz", "___", "r" * 65):
        with pytest.raises(templates.TemplateError):
            templates.layout_template(bad)
    assert set(TEMPLATES) == {"layout", "channels"}
    assert TEMPLATES["channels"].dynamic and templates.channels_template(3).footprint == 3


def test_template_offsets_and_errors():
    t = templates.parse_template({"type": "x", "channels": [{"id": "a"}, {"id": "b", "offset": 5}, {"id": "c"}]})
    assert [(c.id, c.offset) for c in t.channels] == [("a", 0), ("b", 5), ("c", 6)] and t.footprint == 7
    with pytest.raises(templates.TemplateError, match="duplicate id"):
        templates.parse_template({"type": "x", "channels": [{"id": "a"}, {"id": "a"}]})
    with pytest.raises(templates.TemplateError, match="type must match"):
        templates.parse_template({"type": "Bad Type", "channels": [{"id": "a"}]})
    with pytest.raises(templates.TemplateError, match="non-empty"):
        templates.parse_template({"type": "x", "channels": []})
    with pytest.raises(templates.TemplateError, match="built in"):
        templates.parse_template({"type": "layout", "channels": [{"id": "a"}]})
    wide = templates.parse_template({"type": "x", "channels": [{"id": "a", "bits": 16}, {"id": "b"}]})
    assert [(c.offset, c.width) for c in wide.channels] == [(0, 2), (2, 1)] and wide.footprint == 3


def test_user_directory_adds_templates(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "par.json").write_text(json.dumps({"type": "par", "channels": [{"id": "w"}]}), encoding="utf-8")
    loaded = templates.load_templates([ROOT / "templates", user])
    assert loaded["par"].footprint == 1 and set(loaded) == {"layout", "channels", "par"}


def test_shipped_config_is_valid_for_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    s = schema.build_schema(TEMPLATES)
    shipped = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))
    jsonschema.validate(shipped, s)
    def with_devices(devices):
        return {"ports": [{**shipped["ports"][0], "devices": devices}]}
    jsonschema.validate(with_devices([{"name": "Фасад", "type": "layout", "layout": "dRGBW", "address": 0, "count": 40},
                                      {"name": "Logo", "type": "layout", "layout": "d", "bits": 16, "id": "logo_main"}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_devices([{"name": "x", "id": "Bad", "type": "layout"}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_devices([{"name": "x", "type": "layout", "layout": "rgbx"}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_devices([{"name": "x", "type": "nope"}]), s)
    with pytest.raises(jsonschema.ValidationError):                  # a name is required, an id is not
        jsonschema.validate(with_devices([{"type": "layout"}]), s)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"ports": []}, s)


def test_schema_lists_template_types_with_titles_and_translations():
    s = schema.build_schema(TEMPLATES)
    port = s["properties"]["ports"]["items"]
    dev = port["properties"]["devices"]["items"]["properties"]["type"]
    assert dev["enum"] == ["layout", "channels"] and dev["options"]["enum_titles"] == ["Layout", "Channels"]
    props = port["properties"]["devices"]["items"]["properties"]
    assert props["channels"]["options"]["dependencies"] == {"type": ["channels"]}
    assert props["layout"]["options"]["dependencies"] == {"type": ["layout"]} and props["layout"]["default"] == "rgbw"
    assert props["bits"]["enum"] == [8, 16]
    assert s["translations"]["ru"]["Layout"] == "Раскладка"
    assert s["configFile"] == {"path": "/etc/wb-mqtt-dmx.conf", "service": "wb-mqtt-dmx"}
    adv = port["properties"]["settings"]["properties"]["advanced"]
    assert port["title"] == "Port" and s["translations"]["ru"]["Ports"] == "Порты"
    assert s["properties"]["ports"]["_format"] == "tabs-top" and port["properties"]["devices"]["_format"] == "table"
    settings = port["properties"]["settings"]["properties"]
    assert "enabled" not in settings                     # the checkbox sits above the collapsed settings
    block = port["properties"]["settings"]
    assert block["title"] == "Port settings" and "headerTemplate" not in block
    assert s["translations"]["ru"]["Port settings"] == "Настройки порта"
    assert port["properties"]["enabled"]["propertyOrder"] == 0 and port["properties"]["enabled"]["default"] is False
    assert s["translations"]["ru"][port["properties"]["enabled"]["description"]]
    shipped = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))
    assert [p["settings"]["path"] for p in shipped["ports"]] == ["/dev/ttyRS485-1", "/dev/ttyRS485-2"]
    assert not any(p["enabled"] for p in shipped["ports"])
    assert adv["options"]["collapsed"] is True and "start_scene" not in adv["properties"]


def test_every_port_setting_is_explained_in_both_languages():
    s = schema.build_schema(TEMPLATES)
    ru = s["translations"]["ru"]
    settings = s["properties"]["ports"]["items"]["properties"]["settings"]["properties"]
    network, advanced = settings["network"], settings["advanced"]
    fields = {**settings, **network["properties"], **advanced["properties"]}
    for key, field in fields.items():
        text = field.get("description")
        assert text, f"{key} has no caption"
        assert ru.get(text), f"{key}: the caption is not translated"
    for key in ("refresh_hz", "on_start", "on_stop", "slots", "break_us", "mab_us", "slot_gap_us"):
        assert "Default" in fields[key]["description"] and "По умолчанию" in ru[fields[key]["description"]], key
    # the form shows only what the config has: nothing may hide a setting, and the shipped config has them all
    shipped = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))
    port_settings = s["properties"]["ports"]["items"]["properties"]["settings"]
    assert port_settings["required"] == ["path"] and "required" not in network and "required" not in advanced
    for block, path in ((port_settings, ()), (network, ("network",)), (advanced, ("advanced",))):
        for port in shipped["ports"]:
            data = port["settings"]
            for key in path:
                data = data[key]
            assert sorted(data) == sorted(block["properties"]), path
    assert not any("dependencies" in f.get("options", {}) for f in fields.values())
    # network input is always in sight, the timing knobs stay folded away
    assert network["options"].get("disable_collapse") is True and "collapsed" not in network["options"]
    assert advanced["options"]["collapsed"] is True


def test_write_schema_skips_unchanged(tmp_path):
    s = schema.build_schema(TEMPLATES)
    p = tmp_path / "s.json"
    assert schema.write_schema(s, p) is True
    assert schema.write_schema(s, p) is False
