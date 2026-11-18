import json
from pathlib import Path
from typing import Any

import pytest

from wb.mqtt_dmx.dmx_port import DmxPortError
from wb.mqtt_dmx.netin import SACN_OPT_TERMINATED
from wb.mqtt_dmx.service import SERVICE_DEVICE, CheckedPort, serial_port_conflict

from .fakes import PORT_PATH, make_service
from .packets import artdmx_packet, sacn_packet

STATUS = f"/devices/{SERVICE_DEVICE}/controls"
SPAN_AND_LOGO: list[dict[str, Any]] = [
    {"name": "Пролёт", "layout": "rgbw", "count": 2},
    {"name": "Логотип", "layout": "d"},
]


def saved_state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def test_devices_groups_and_their_controls_are_published(tmp_path: Path) -> None:
    _service, topics = make_service(SPAN_AND_LOGO, tmp_path)

    for device in ("dmx_prolet", "dmx_prolet1", "dmx_prolet2", "dmx_logotip"):
        assert topics[f"/devices/{device}/meta"]
    assert json.loads(topics["/devices/dmx_prolet1/controls/red/meta"])["title"] == {
        "en": "Red",
        "ru": "Красный",
    }
    assert [topics[f"/devices/dmx_prolet1/controls/{c}"] for c in ("red", "green", "blue", "white")] == [
        "0"
    ] * 4
    assert topics["/devices/dmx_prolet2/controls/dmx"] == "DMX 5–8"
    assert topics["/devices/dmx_logotip/controls/dmx"] == "DMX 9"


def test_service_device_shows_the_port_and_its_address_map(tmp_path: Path) -> None:
    _service, topics = make_service(SPAN_AND_LOGO, tmp_path)

    assert json.loads(topics[f"/devices/{SERVICE_DEVICE}/meta"])["driver"] == "wb-mqtt-dmx"
    assert topics[f"{STATUS}/status"] == "0"
    assert topics[f"{STATUS}/rs485_2_map"] == "Пролёт: 1-8 (2 × 4); Логотип: 9"
    assert topics[f"{STATUS}/rs485_2_state"] == "1", "starting: tests run no frame thread"
    assert topics[f"{STATUS}/rs485_2_error"] == ""
    assert json.loads(topics[f"{STATUS}/rs485_2_state/meta"])["enum"]["2"] == {
        "en": "Running",
        "ru": "Работает",
    }


def test_write_lands_in_the_frame_and_in_the_control(tmp_path: Path) -> None:
    service, topics = make_service(SPAN_AND_LOGO, tmp_path)

    assert service.handle_write("dmx_prolet2", "green", "200")
    assert service.frame(PORT_PATH)[:9] == bytes([0, 0, 0, 0, 0, 200, 0, 0, 0])
    assert topics["/devices/dmx_prolet2/controls/green"] == "200"


def test_write_to_a_group_reaches_every_member(tmp_path: Path) -> None:
    service, topics = make_service(SPAN_AND_LOGO, tmp_path)

    assert service.handle_write("dmx_prolet", "white", "17")
    assert (service.frame(PORT_PATH)[3], service.frame(PORT_PATH)[7]) == (17, 17)
    assert topics["/devices/dmx_prolet1/controls/white"] == "17"


def test_written_value_is_clamped(tmp_path: Path) -> None:
    service, _topics = make_service(SPAN_AND_LOGO, tmp_path)

    assert service.handle_write("dmx_logotip", "dimmer", "999")
    assert service.frame(PORT_PATH)[8] == 255


@pytest.mark.parametrize(
    "device, control, payload",
    [("dmx_nope", "dimmer", "1"), ("dmx_logotip", "nope", "1"), ("dmx_logotip", "dimmer", "abc")],
)
def test_write_to_nowhere_or_of_garbage_is_refused(
    tmp_path: Path, device: str, control: str, payload: str
) -> None:
    service, _topics = make_service(SPAN_AND_LOGO, tmp_path)

    assert not service.handle_write(device, control, payload)
    assert service.frame(PORT_PATH) == bytes(512)


def test_blackout_zeroes_the_frame_and_the_controls(tmp_path: Path) -> None:
    service, topics = make_service(SPAN_AND_LOGO, tmp_path)
    service.handle_write("dmx_prolet2", "green", "200")

    assert service.handle_write(SERVICE_DEVICE, "blackout", "1")
    assert service.frame(PORT_PATH) == bytes(512)
    assert topics["/devices/dmx_prolet2/controls/green"] == "0"


def test_values_are_saved_and_restored_on_the_next_start(tmp_path: Path) -> None:
    devices = [
        {"name": "Логотип", "layout": "d"},
        {"name": "Дым", "type": "channels", "channels": 2},
        {"name": "Точный", "layout": "dw", "bits": 16},
    ]
    service, _topics = make_service(devices, tmp_path)
    service.handle_write("dmx_logotip", "dimmer", "123")
    service.handle_write("dmx_dym", "ch2", "9")
    service.handle_write("dmx_tochnyy", "dimmer", "65535")
    service.handle_write("dmx_tochnyy", "white", "258")
    service.save_state()

    assert saved_state(tmp_path) == {PORT_PATH: {"1": 123, "3": 9, "4": 255, "5": 255, "6": 1, "7": 2}}
    restarted, topics = make_service(devices, tmp_path)
    assert restarted.frame(PORT_PATH)[0] == 123
    assert topics["/devices/dmx_logotip/controls/dimmer"] == "123"
    assert topics["/devices/dmx_dym/controls/ch2"] == "9"
    assert topics["/devices/dmx_tochnyy/controls/dimmer"] == "65535"
    assert topics["/devices/dmx_tochnyy/controls/white"] == "258"


def test_console_on_the_network_takes_the_port_over(tmp_path: Path) -> None:
    service, topics = make_service(SPAN_AND_LOGO, tmp_path, network={"artnet": True, "artnet_universe": 5})
    assert topics[f"{STATUS}/rs485_2_source"] == "MQTT"
    assert service.net is not None

    service.net.feed("artnet", artdmx_packet(5, bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0])), "10.0.0.1")
    service.publish_net_values()

    assert topics[f"{STATUS}/rs485_2_source"] == "Art-Net 10.0.0.1 · 0 fps"
    assert service.frame(PORT_PATH)[:9] == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert topics["/devices/dmx_prolet1/controls/red"] == "1"
    assert topics["/devices/dmx_prolet2/controls/white"] == "8"
    assert topics["/devices/dmx_prolet/controls/green"] == "2", "a group shows its first member"
    assert topics["/devices/dmx_logotip/controls/dimmer"] == "9"


def test_mqtt_values_are_kept_aside_while_a_console_owns_the_port(tmp_path: Path) -> None:
    service, topics = make_service(SPAN_AND_LOGO, tmp_path, network={"artnet": True, "artnet_universe": 5})
    assert service.net is not None
    service.handle_write("dmx_prolet1", "red", "10")
    service.net.feed("artnet", artdmx_packet(5, bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0])), "10.0.0.1")
    service.handle_write("dmx_logotip", "dimmer", "77")
    service.save_state()

    assert saved_state(tmp_path)[PORT_PATH] == {"1": 10, "9": 77}, "the MQTT values, not the console's frame"
    assert service.frame(PORT_PATH)[8] == 9, "the console still draws"
    assert topics["/devices/dmx_logotip/controls/dimmer"] == "77"


def test_port_returns_to_the_mqtt_values_when_the_console_stops(tmp_path: Path) -> None:
    service, topics = make_service(SPAN_AND_LOGO, tmp_path, network={"sacn": True, "sacn_universe": 5})
    assert service.net is not None
    service.handle_write("dmx_prolet1", "red", "10")
    service.net.feed("sacn", sacn_packet(5, bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])), "10.0.0.1")
    service.publish_net_values()
    assert topics["/devices/dmx_prolet1/controls/red"] == "1"

    service.net.feed("sacn", sacn_packet(5, b"", options=SACN_OPT_TERMINATED), "10.0.0.1")

    assert topics[f"{STATUS}/rs485_2_source"] == "MQTT"
    assert service.frame(PORT_PATH)[:9] == bytes([10, 0, 0, 0, 0, 0, 0, 0, 0])
    assert topics["/devices/dmx_prolet1/controls/red"] == "10"
    assert topics["/devices/dmx_prolet2/controls/white"] == "0"


def test_disabled_port_is_listed_but_has_no_devices(tmp_path: Path) -> None:
    _service, topics = make_service([{"name": "Логотип", "layout": "d"}], tmp_path, enabled=False)

    assert topics[f"{STATUS}/rs485_2_state"] == "0"
    assert topics[f"{STATUS}/status"] == "2"
    assert "/devices/dmx_logotip/meta" not in topics


@pytest.mark.parametrize(
    "serial_port, conflict",
    [
        ({"path": PORT_PATH, "enabled": True}, True),
        ({"path": PORT_PATH}, True),
        ({"path": PORT_PATH, "enabled": False}, False),
    ],
)
def test_port_enabled_in_wb_mqtt_serial_is_a_conflict(
    tmp_path: Path, serial_port: dict[str, Any], conflict: bool
) -> None:
    conf = tmp_path / "serial.conf"
    conf.write_text(json.dumps({"ports": [serial_port]}), encoding="utf-8")

    assert ("enabled in wb-mqtt-serial" in (serial_port_conflict(PORT_PATH, conf) or "")) is conflict


def test_missing_serial_config_is_no_conflict(tmp_path: Path) -> None:
    assert serial_port_conflict(PORT_PATH, tmp_path / "missing.conf") is None


def test_port_owned_by_wb_mqtt_serial_does_not_open(tmp_path: Path) -> None:
    conf = tmp_path / "serial.conf"
    conf.write_text(json.dumps({"ports": [{"path": PORT_PATH}]}), encoding="utf-8")

    with pytest.raises(DmxPortError, match="enabled in wb-mqtt-serial"):
        CheckedPort(PORT_PATH, serial_conf=conf).open()
