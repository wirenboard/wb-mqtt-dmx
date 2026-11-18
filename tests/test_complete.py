"""A config written by hand or by an older version gets the port settings it lacks (the form shows only those)."""

import json

from wb.mqtt_dmx import schema, templates
from wb.mqtt_dmx.config import complete_settings, load_config
from wb.mqtt_dmx.main import complete_config_file

TEMPLATES = templates.load_templates([])


def old_config():
    return {"ports": [
        {"settings": {"path": "/dev/ttyRS485-1", "enabled": True, "network": {"artnet": False, "sacn": False},
                      "advanced": {"slots": 128, "break_us": 200, "mab_us": 20, "de_control": "rts"}},
         "devices": [{"name": "Пар", "type": "layout", "layout": "rgb"}]},
        {"settings": {"path": "/dev/ttyRS485-2", "name": "Сцена", "refresh_hz": 30}, "devices": []},
    ]}


def test_missing_settings_get_their_defaults_and_nothing_else_changes():
    cfg = old_config()
    assert complete_settings(cfg) > 0
    first, second = (p["settings"] for p in cfg["ports"])
    assert first["network"] == {"artnet": False, "sacn": False, "artnet_universe": 0, "sacn_universe": 1}
    # the obsolete transmitter setting goes away: the form would show a leftover key as a nameless field
    assert first["advanced"] == {"slots": 128, "break_us": 200, "mab_us": 20, "slot_gap_us": 0}
    assert first["enabled"] is True and first["name"] == "DMX 1" and first["refresh_hz"] == 40
    assert second["name"] == "Сцена" and second["refresh_hz"] == 30 and second["enabled"] is False
    assert second["network"]["artnet_universe"] == 1 and second["network"]["sacn_universe"] == 2   # own numbers
    assert second["advanced"]["slots"] == 512
    assert cfg["ports"][0]["devices"] == [{"name": "Пар", "type": "layout", "layout": "rgb"}]
    assert complete_settings(cfg) == 0                         # nothing to add the second time
    assert len(load_config(cfg, TEMPLATES)) == 2


def test_completed_config_has_every_setting_of_the_form():
    cfg = old_config()
    complete_settings(cfg)
    form = schema.build_schema(TEMPLATES)["properties"]["ports"]["items"]["properties"]["settings"]["properties"]
    for port in cfg["ports"]:
        settings = port["settings"]
        assert sorted(settings) == sorted(form)
        assert sorted(settings["network"]) == sorted(form["network"]["properties"])
        assert sorted(settings["advanced"]) == sorted(form["advanced"]["properties"])


def test_garbage_is_left_alone():
    for junk in (None, [], {"ports": "x"}, {"ports": [1, {"settings": 5}]}):
        assert complete_settings(junk) == 0


def test_config_file_is_completed_in_place(tmp_path, capsys):
    path = tmp_path / "wb-mqtt-dmx.conf"
    path.write_text(json.dumps(old_config(), ensure_ascii=False), encoding="utf-8")
    assert complete_config_file(path) > 0
    assert "missing setting" in capsys.readouterr().out
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ports"][0]["settings"]["advanced"]["slot_gap_us"] == 0 and saved["ports"][1]["settings"]["name"] == "Сцена"
    before = path.read_text(encoding="utf-8")
    assert complete_config_file(path) == 0 and path.read_text(encoding="utf-8") == before
    assert complete_config_file(tmp_path / "absent.conf") == 0
    broken = tmp_path / "broken.conf"
    broken.write_text("{not json", encoding="utf-8")
    assert complete_config_file(broken) == 0 and broken.read_text(encoding="utf-8") == "{not json"
