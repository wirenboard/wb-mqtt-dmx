"""The service against a fake MQTT client and a fake port: topics, writes, groups, state, blackout."""

import json
from pathlib import Path

from wb.mqtt_dmx import templates
from wb.mqtt_dmx.config import load_config
from wb.mqtt_dmx.service import SERVICE_DEVICE, CheckedPort, DmxService, serial_port_conflict

ROOT = Path(__file__).parent.parent
TEMPLATES = templates.load_templates([ROOT / "templates"])


class FakeClient:
    def __init__(self, _client_id):
        self.topics = {}
        self.on_connect = None
        self.on_message = None

    def will_set(self, *_a, **_k):
        pass

    def subscribe(self, *_a, **_k):
        pass

    def publish(self, topic, payload, retain=True):
        self.topics[topic] = payload

    def start(self):
        self.on_connect(self, None, None, 0)

    def stop(self):
        pass


def make(cfg, tmp_path, enabled=True):
    cfg = {"ports": [{"settings": {"enabled": enabled, "path": "/dev/ttyRS485-2", "name": "Фасад", "refresh_hz": 40,
                                   "on_start": "restore", "on_stop": "hold",
                                   "advanced": {"slots": 512}},
                      "devices": cfg}]}
    ports = load_config(cfg, TEMPLATES)
    svc = DmxService(ports, client_factory=FakeClient, serial_conf=tmp_path / "serial.conf",
                     state_path=tmp_path / "state.json", install_signals=False, realtime=False)
    svc._client.start()          # publishes everything as a real connect would
    return svc, svc._client.topics


def test_devices_and_groups_are_published(tmp_path):
    svc, t = make([{"name": "Пролёт", "type": "rgbw", "count": 2}, {"name": "Логотип", "type": "dimmer"}], tmp_path)
    assert json.loads(t[f"/devices/{SERVICE_DEVICE}/meta"])["driver"] == "wb-mqtt-dmx"
    assert t[f"/devices/{SERVICE_DEVICE}/controls/rs485_2_map"] == "Пролёт: 1-8 (2 × 4); Логотип: 9"
    assert t["/devices/dmx_prolet/meta"] and t["/devices/dmx_prolet1/meta"] and t["/devices/dmx_logotip/meta"]
    assert json.loads(t["/devices/dmx_prolet1/controls/color/meta"])["type"] == "rgb"
    assert t["/devices/dmx_prolet1/controls/color"] == "0;0;0"
    assert json.loads(t["/devices/dmx_prolet1/controls/white/meta"])["title"] == {"en": "White", "ru": "Белый"}
    assert "/devices/dmx_prolet1/controls/red" not in t
    assert t["/devices/dmx_prolet2/controls/dmx"] == "DMX 5–8"
    assert json.loads(t["/devices/dmx_prolet2/controls/dmx/meta"])["title"]["ru"] == "RGBW (4 канала)"
    assert t["/devices/dmx_logotip/controls/dmx"] == "DMX 9"
    assert t[f"/devices/{SERVICE_DEVICE}/controls/status"] == "0"
    state = json.loads(t[f"/devices/{SERVICE_DEVICE}/controls/rs485_2_state/meta"])
    assert state["enum"]["2"] == {"en": "Running", "ru": "Работает"}
    assert t[f"/devices/{SERVICE_DEVICE}/controls/rs485_2_state"] == "1"       # starting: no frame thread in tests
    assert t[f"/devices/{SERVICE_DEVICE}/controls/rs485_2_error"] == ""
    assert "/devices/dmx_rs485_2/meta" not in t


def test_writes_land_in_the_frame_buffer_and_groups_fan_out(tmp_path):
    svc, t = make([{"name": "Пролёт", "type": "rgbw", "count": 2}, {"name": "Логотип", "type": "dimmer"}], tmp_path)
    universe = svc._runtimes["/dev/ttyRS485-2"].universe
    assert svc.handle_write("dmx_prolet2", "color", "10;200;30")
    assert universe.snapshot()[:9] == bytes([0, 0, 0, 0, 10, 200, 30, 0, 0])
    assert t["/devices/dmx_prolet2/controls/color"] == "10;200;30"
    assert svc.handle_write("dmx_prolet", "white", "17")             # the group writes both members
    assert universe.snapshot()[3] == 17 and universe.snapshot()[7] == 17
    assert t["/devices/dmx_prolet1/controls/white"] == "17"
    assert svc.handle_write("dmx_logotip", "dimmer", "999") and universe.get(9) == 255   # clamped
    assert not svc.handle_write("dmx_prolet2", "color", "1;2")
    assert not svc.handle_write("dmx_nope", "dimmer", "1")
    assert not svc.handle_write("dmx_logotip", "nope", "1")
    assert not svc.handle_write("dmx_logotip", "dimmer", "abc")
    assert svc.handle_write(SERVICE_DEVICE, "blackout", "1")
    assert universe.snapshot() == bytes(512) and t["/devices/dmx_prolet2/controls/color"] == "0;0;0"


def test_state_is_saved_and_restored(tmp_path):
    devices = [{"name": "Логотип", "type": "dimmer"}, {"name": "Пар", "type": "rgb"}, {"name": "Дым", "channels": 2}]
    svc, _ = make(devices, tmp_path)
    svc.handle_write("dmx_logotip", "dimmer", "123")
    svc.handle_write("dmx_par", "color", "1;2;3")
    svc.handle_write("dmx_dym", "ch2", "9")
    svc._save_state()
    assert json.loads((tmp_path / "state.json").read_text()) == {
        "/dev/ttyRS485-2": {"1": 123, "2": 1, "3": 2, "4": 3, "6": 9}}
    svc2, t2 = make(devices, tmp_path)
    assert svc2._runtimes["/dev/ttyRS485-2"].universe.get(1) == 123
    assert t2["/devices/dmx_logotip/controls/dimmer"] == "123"
    assert t2["/devices/dmx_par/controls/color"] == "1;2;3" and t2["/devices/dmx_dym/controls/ch2"] == "9"
    assert json.loads(t2["/devices/dmx_dym/controls/dmx/meta"])["title"]["ru"] == "Каналы"


def test_disabled_port_shows_in_status_without_devices(tmp_path):
    svc, t = make([{"name": "Логотип", "type": "dimmer"}], tmp_path, enabled=False)
    assert t[f"/devices/{SERVICE_DEVICE}/controls/rs485_2_state"] == "0"
    assert t[f"/devices/{SERVICE_DEVICE}/controls/status"] == "2"
    assert "/devices/dmx_logotip/meta" not in t


def test_port_still_owned_by_wb_mqtt_serial_is_refused(tmp_path):
    conf = tmp_path / "serial.conf"
    conf.write_text(json.dumps({"ports": [{"path": "/dev/ttyRS485-2", "enabled": True}]}), encoding="utf-8")
    assert "enabled in wb-mqtt-serial" in serial_port_conflict("/dev/ttyRS485-2", conf)
    conf.write_text(json.dumps({"ports": [{"path": "/dev/ttyRS485-2", "enabled": False}]}), encoding="utf-8")
    assert serial_port_conflict("/dev/ttyRS485-2", conf) is None
    assert serial_port_conflict("/dev/ttyRS485-2", tmp_path / "missing.conf") is None
    port = CheckedPort("/dev/ttyRS485-2", serial_conf=conf)
    conf.write_text(json.dumps({"ports": [{"path": "/dev/ttyRS485-2"}]}), encoding="utf-8")   # enabled by default
    try:
        port.open()
    except Exception as e:  # DmxPortError
        assert "enabled in wb-mqtt-serial" in str(e)
    else:
        raise AssertionError("open() must refuse a port owned by wb-mqtt-serial")
