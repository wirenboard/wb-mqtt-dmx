"""The modes string: named ranges of the m channel become a drop-down control."""

import json
import re

import pytest

from wb.mqtt_dmx import templates
from wb.mqtt_dmx.config import ConfigError, load_config
from wb.mqtt_dmx.controls import build_controls

from test_service import TEMPLATES, make

MODES = "0 Ручной, 51 Скачки; 101-150: Перелив, 151 = Пульсация, 201 Звук"


def test_modes_string_gives_ranges():
    ranges = templates.parse_modes(MODES)
    assert [(r.start, r.end, r.value) for r in ranges] == [(0, 50, 25), (51, 100, 75), (101, 150, 125),
                                                           (151, 200, 175), (201, 255, 228)]
    assert [r.title["ru"] for r in ranges] == ["Ручной", "Скачки", "Перелив", "Пульсация", "Звук"]
    assert templates.parse_modes("") == [] and templates.parse_modes(" , ") == []
    for bad, why in (("5 Поздно", "start at 0"), ("0 a, 0 b", "start after"), ("0 a, 300 b", "out of the range"),
                     ("просто текст", "value name"), ("0-999 a", "out of the range")):
        with pytest.raises(templates.TemplateError, match=why):
            templates.parse_modes(bad)


def test_form_pattern_accepts_what_the_parser_accepts():
    pattern = re.compile(templates.MODES_RE)
    for good in ("", MODES, "0 Manual", "0-50 Manual; 51-100 Jump", "0: off, 11: on"):
        assert pattern.match(good), good
    for bad in ("просто текст", "0", "51"):
        assert pattern.match(bad) is None, bad


def test_mode_channel_is_a_drop_down_control():
    t = templates.layout_template("drgbwms", 8, MODES)
    controls = {c.id: c for c in build_controls(t)}
    mode = controls["mode"]
    meta = mode.meta(6)
    assert meta["type"] == "value" and meta["readonly"] is False and "max" not in meta
    assert meta["enum"] == {"25": {"en": "Ручной", "ru": "Ручной"}, "75": {"en": "Скачки", "ru": "Скачки"},
                            "125": {"en": "Перелив", "ru": "Перелив"}, "175": {"en": "Пульсация", "ru": "Пульсация"},
                            "228": {"en": "Звук", "ru": "Звук"}}
    assert mode.default == 25 and mode.zero == 25
    assert [mode.parse(v) for v in ("0", "50", "51", "60", "200", "255", "x")] == [25, 25, 75, 75, 175, 228, None]
    assert mode.value_from({5: 140}) == 125
    assert controls["strobe"].meta(7)["type"] == "range" and controls["strobe"].parse("60") == 60
    plain = build_controls(templates.layout_template("drgbwms"))[5]       # no modes: the slider stays
    assert plain.meta(6)["type"] == "range" and plain.parse("60") == 60


def test_modes_without_an_m_channel_are_ignored():
    t = templates.layout_template("drgbw_s", 8, MODES)             # the m was removed, the modes text stayed
    assert all(not c.ranges for c in t.channels) and t.footprint == 7
    assert templates.layout_template("rgb", 8, "не разобрать").footprint == 3
    cfg = {"ports": [{"settings": {"path": "/dev/ttyRS485-2"},
                      "devices": [{"name": "Пар", "layout": "rgb", "modes": "0 Ручной"}]}]}
    (port,) = load_config(cfg, TEMPLATES)
    assert [c.id for c in port.devices[0].template.channels] == ["red", "green", "blue"]
    with pytest.raises(templates.TemplateError, match="8-bit"):
        templates.layout_template("dm", 16, MODES)
    cfg["ports"][0]["devices"][0].update(layout="rgbm", modes="5 Поздно")
    with pytest.raises(ConfigError, match="start at 0"):
        load_config(cfg, TEMPLATES)


def test_service_publishes_the_list_and_snaps_writes(tmp_path):
    svc, t = make([{"name": "Пар", "layout": "drgbwms", "modes": MODES, "count": 2}], tmp_path)
    assert json.loads(t["/devices/dmx_par1/controls/mode/meta"])["enum"]["75"]["ru"] == "Скачки"
    assert t["/devices/dmx_par1/controls/mode"] == "25"
    universe = svc._runtimes["/dev/ttyRS485-2"].universe
    assert svc.handle_write("dmx_par2", "mode", "60")                # a rule may write any number of the range
    assert t["/devices/dmx_par2/controls/mode"] == "75" and universe.snapshot()[7 + 5] == 75
    assert svc.handle_write("dmx_par", "mode", "228")                # the group sets both
    assert universe.snapshot()[5] == 228 and universe.snapshot()[12] == 228
    svc.blackout()
    assert t["/devices/dmx_par1/controls/mode"] == "25" and universe.snapshot()[5] == 0
