import json
from pathlib import Path

import pytest

from wb.mqtt_dmx import templates
from wb.mqtt_dmx.config import ConfigError, address_map, load_config

ROOT = Path(__file__).parent.parent
TEMPLATES = templates.load_templates([ROOT / "templates"])


def port(path="/dev/ttyRS485-2", devices=(), **settings):
    base = {"enabled": True, "path": path, "name": "DMX", "refresh_hz": 40, "on_start": "restore", "on_stop": "hold",
            "advanced": {"slots": 512, "break_us": 176, "mab_us": 20, "de_control": "rts"}}
    base.update(settings)
    return {"settings": base, "devices": list(devices)}


def dev(name, type_="rgbw", **kw):
    return {"name": name, "type": type_, **kw}


def test_shipped_config_loads_with_two_disabled_ports():
    cfg = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))
    ports = load_config(cfg, TEMPLATES)
    assert [(p.path, p.enabled, p.key) for p in ports] == [
        ("/dev/ttyRS485-1", False, "rs485_1"), ("/dev/ttyRS485-2", False, "rs485_2")]


def test_ids_come_from_names_and_addresses_follow_each_other():
    cfg = {"ports": [port(devices=[dev("Фасад", count=12), dev("Логотип", "dimmer"), dev("Вывеска", "cct", address=100)])]}
    (p,) = load_config(cfg, TEMPLATES)
    assert [(d.id, d.address) for d in p.devices][:2] == [("fasad1", 1), ("fasad2", 5)]
    assert [(d.id, d.name, d.address) for d in p.devices][12:] == [("logotip", "Логотип", 49), ("vyveska", "Вывеска", 100)]
    (g,) = p.groups
    assert g.device_id == "dmx_fasad" and g.last_address == 48 and all(d.group == "fasad" for d in g.members)
    assert address_map(p) == "Фасад: 1-48 (12 × 4); Логотип: 49; Вывеска: 100-101"


def test_pinned_id_and_derived_collisions():
    cfg = {"ports": [port(devices=[dev("Logo", id="logo_main"), dev("Лого", "dimmer")])]}
    (p,) = load_config(cfg, TEMPLATES)
    assert [d.id for d in p.devices] == ["logo_main", "logo"]
    cfg = {"ports": [port(devices=[dev("Logo"), dev("Logo!", "dimmer")])]}    # both derive to "logo"
    (p,) = load_config(cfg, TEMPLATES)
    assert [d.id for d in p.devices] == ["logo", "logo_2"]
    with pytest.raises(ConfigError, match="id 'logo' is already used"):
        load_config({"ports": [port(devices=[dev("Logo"), dev("Other", id="logo")])]}, TEMPLATES)


def test_repeated_names_are_rejected_across_ports():
    cfg = {"ports": [port("/dev/ttyRS485-1", [dev("Фасад")]), port("/dev/ttyRS485-2", [dev("фасад")])]}
    with pytest.raises(ConfigError, match="name 'фасад' is used twice"):
        load_config(cfg, TEMPLATES)


def test_overlaps_overflow_and_unknown_type():
    with pytest.raises(ConfigError, match="overlap"):
        load_config({"ports": [port(devices=[dev("A", address=1), dev("B", address=3)])]}, TEMPLATES)
    with pytest.raises(ConfigError, match="past the 512 channels"):
        load_config({"ports": [port(devices=[dev("A", count=129)])]}, TEMPLATES)
    with pytest.raises(ConfigError, match="unknown type 'nope'"):
        load_config({"ports": [port(devices=[dev("A", "nope")])]}, TEMPLATES)
    with pytest.raises(ConfigError, match="listed twice"):
        load_config({"ports": [port(), port()]}, TEMPLATES)
    with pytest.raises(ConfigError, match="refresh_hz must be 1..44"):
        load_config({"ports": [port(refresh_hz=60)]}, TEMPLATES)


def test_device_rows_must_differ_for_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    from wb.mqtt_dmx import schema
    s = schema.build_schema(TEMPLATES)
    same = {"name": "Device", "type": "dimmer", "address": 0, "count": 1, "id": ""}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"ports": [port(devices=[same, dict(same)])]}, s)
    jsonschema.validate({"ports": [port(devices=[same, {**same, "address": 5}])]}, s)
