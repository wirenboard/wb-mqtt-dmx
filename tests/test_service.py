import json
from pathlib import Path
from typing import Any

import pytest

from wb.mqtt_dmx.controls import ControlSpec
from wb.mqtt_dmx.dmx_port import DmxPortError
from wb.mqtt_dmx.netin import SACN_OPT_TERMINATED
from wb.mqtt_dmx.service import SERVICE_DEVICE, CheckedPort, serial_port_conflict

from .fakes import PORT_PATH, make_service, shown
from .packets import artdmx_packet, sacn_packet

SPAN_AND_LOGO: list[dict[str, Any]] = [
    {"name": "Пролёт", "layout": "rgbw", "count": 2},
    {"name": "Логотип", "layout": "d"},
]


def saved_state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def test_devices_groups_and_their_controls_are_listed(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    specs = {spec.id: spec for spec in service.device_specs()}

    assert list(specs) == [SERVICE_DEVICE, "dmx_prolet", "dmx_prolet1", "dmx_prolet2", "dmx_logotip"]
    assert specs["dmx_prolet"].title == {"en": "Пролёт (all)", "ru": "Пролёт (все)"}
    assert specs["dmx_prolet1"].title == {"en": "Пролёт 1", "ru": "Пролёт 1"}
    assert specs["dmx_prolet1"].controls[0] == ControlSpec(
        "red", "range", {"en": "Red", "ru": "Красный"}, readonly=False, maximum=255
    )
    assert [c.id for c in specs["dmx_prolet1"].controls] == ["red", "green", "blue", "white", "dmx"]
    assert [shown(service, "dmx_prolet1", c) for c in ("red", "green", "blue", "white")] == [0] * 4
    assert shown(service, "dmx_prolet", "dmx") == "2 × 4 · DMX 1–8"
    assert shown(service, "dmx_prolet2", "dmx") == "DMX 5–8"
    assert shown(service, "dmx_logotip", "dmx") == "DMX 9"


def test_service_device_shows_the_port_and_its_address_map(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    controls = {control.id: control for control in service.device_specs()[0].controls}
    status = service.shown_values()[SERVICE_DEVICE]

    assert list(controls)[0] == "status" and list(controls)[-1] == "blackout"
    assert controls["blackout"].type == "pushbutton" and not controls["blackout"].readonly
    assert controls["rs485_2_state"].enum is not None
    assert controls["rs485_2_state"].enum["2"] == {"en": "Running", "ru": "Работает"}
    assert status["status"] == 0
    assert status["rs485_2_map"] == "Пролёт: 1-8 (2 × 4); Логотип: 9"
    assert status["rs485_2_state"] == 1, "starting: tests run no frame thread"
    assert status["rs485_2_error"] is None
    assert "blackout" not in status, "a button has no value"
    assert service.failed_devices() == set()


def test_write_lands_in_the_frame_and_in_the_control(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    assert service.handle_write("dmx_prolet2", "green", "200")
    assert service.frame(PORT_PATH)[:9] == bytes([0, 0, 0, 0, 0, 200, 0, 0, 0])
    assert shown(service, "dmx_prolet2", "green") == 200


def test_write_to_a_group_reaches_every_member(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    assert service.handle_write("dmx_prolet", "white", "17")
    assert (service.frame(PORT_PATH)[3], service.frame(PORT_PATH)[7]) == (17, 17)
    assert [shown(service, device, "white") for device in ("dmx_prolet", "dmx_prolet1", "dmx_prolet2")] == [
        17
    ] * 3


def test_written_value_is_clamped(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    assert service.handle_write("dmx_logotip", "dimmer", "999")
    assert service.frame(PORT_PATH)[8] == 255
    assert shown(service, "dmx_logotip", "dimmer") == 255


@pytest.mark.parametrize(
    "device, control, payload",
    [
        ("dmx_nope", "dimmer", "1"),
        ("dmx_logotip", "nope", "1"),
        ("dmx_logotip", "dimmer", "abc"),
        ("dmx_logotip", "dmx", "1"),
        (SERVICE_DEVICE, "status", "1"),
    ],
)
def test_write_to_nowhere_or_of_garbage_is_refused(
    tmp_path: Path, device: str, control: str, payload: str
) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)

    assert not service.handle_write(device, control, payload)
    assert service.frame(PORT_PATH) == bytes(512)


def test_blackout_zeroes_the_frame_and_the_controls(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    service.handle_write("dmx_prolet2", "green", "200")

    assert service.handle_write(SERVICE_DEVICE, "blackout", "True")
    assert service.frame(PORT_PATH) == bytes(512)
    assert shown(service, "dmx_prolet2", "green") == 0


def test_values_are_saved_once_the_light_stands_still(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    now = [100.0]
    service.clock = lambda: now[0]
    service.handle_write("dmx_logotip", "dimmer", "50")

    now[0] += 9
    assert not service.save_state_when_settled()
    assert not (tmp_path / "state.json").exists()
    now[0] += 2
    assert service.save_state_when_settled()
    assert saved_state(tmp_path) == {PORT_PATH: {"9": 50}}
    assert not service.save_state_when_settled(), "nothing has changed since"


def test_a_running_scene_is_never_saved(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    now = [100.0]
    service.clock = lambda: now[0]

    for step in range(120):  # two minutes of a value changing every second
        service.handle_write("dmx_logotip", "dimmer", str(step))
        now[0] += 1
        assert not service.save_state_when_settled()

    assert not (tmp_path / "state.json").exists()


def test_repeating_the_same_value_does_not_hold_the_save_back(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    now = [100.0]
    service.clock = lambda: now[0]
    service.handle_write("dmx_prolet", "red", "7")

    for _ in range(3):  # a rule sends everything again every five seconds
        now[0] += 5
        service.handle_write("dmx_prolet", "red", "7")
        service.handle_write("dmx_prolet2", "red", "7")

    assert service.save_state_when_settled()
    assert saved_state(tmp_path) == {PORT_PATH: {"1": 7, "5": 7}}


def test_unchanged_values_are_not_written_again(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path)
    service.handle_write("dmx_logotip", "dimmer", "50")
    service.save_state()
    (tmp_path / "state.json").unlink()

    service.handle_write("dmx_logotip", "dimmer", "60")
    service.handle_write("dmx_logotip", "dimmer", "50")
    service.save_state()

    assert not (tmp_path / "state.json").exists(), "the file already holds these values"


def test_port_that_starts_dark_stores_nothing(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path, on_start="zero")
    service.handle_write("dmx_logotip", "dimmer", "50")
    service.save_state()

    assert not (tmp_path / "state.json").exists()


def test_values_are_saved_and_restored_on_the_next_start(tmp_path: Path) -> None:
    devices = [
        {"name": "Логотип", "layout": "d"},
        {"name": "Дым", "type": "channels", "channels": 2},
        {"name": "Точный", "layout": "dw", "bits": 16},
    ]
    service = make_service(devices, tmp_path)
    service.handle_write("dmx_logotip", "dimmer", "123")
    service.handle_write("dmx_dym", "ch2", "9")
    service.handle_write("dmx_tochnyy", "dimmer", "65535")
    service.handle_write("dmx_tochnyy", "white", "258")
    service.save_state()

    assert saved_state(tmp_path) == {PORT_PATH: {"1": 123, "3": 9, "4": 255, "5": 255, "6": 1, "7": 2}}
    restarted = make_service(devices, tmp_path)
    assert restarted.frame(PORT_PATH)[0] == 123
    assert shown(restarted, "dmx_logotip", "dimmer") == 123
    assert shown(restarted, "dmx_dym", "ch2") == 9
    assert shown(restarted, "dmx_tochnyy", "dimmer") == 65535
    assert shown(restarted, "dmx_tochnyy", "white") == 258


def test_console_on_the_network_takes_the_port_over(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path, network={"artnet": True, "artnet_universe": 5})
    changes: list[int] = []
    service.on_change = lambda: changes.append(1)
    assert shown(service, SERVICE_DEVICE, "rs485_2_source") == "MQTT"
    assert service.net is not None

    service.net.feed("artnet", artdmx_packet(5, bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0])), "10.0.0.1")

    assert changes == [1], "whoever publishes the devices is told at once"
    assert shown(service, SERVICE_DEVICE, "rs485_2_source") == "Art-Net 10.0.0.1 · 0 fps"
    assert service.frame(PORT_PATH)[:9] == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert shown(service, "dmx_prolet1", "red") == 1
    assert shown(service, "dmx_prolet2", "white") == 8
    assert shown(service, "dmx_prolet", "green") == 2, "a group shows its first member"
    assert shown(service, "dmx_logotip", "dimmer") == 9


def test_mqtt_values_are_kept_aside_while_a_console_owns_the_port(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path, network={"artnet": True, "artnet_universe": 5})
    assert service.net is not None
    service.handle_write("dmx_prolet1", "red", "10")
    service.net.feed("artnet", artdmx_packet(5, bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0])), "10.0.0.1")

    assert service.handle_write("dmx_logotip", "dimmer", "77")
    service.save_state()

    assert saved_state(tmp_path)[PORT_PATH] == {"1": 10, "9": 77}, "the MQTT values, not the console's frame"
    assert service.frame(PORT_PATH)[8] == 9, "the console still draws"
    assert shown(service, "dmx_logotip", "dimmer") == 9, "and the control shows what it draws"


def test_port_returns_to_the_mqtt_values_when_the_console_stops(tmp_path: Path) -> None:
    service = make_service(SPAN_AND_LOGO, tmp_path, network={"sacn": True, "sacn_universe": 5})
    assert service.net is not None
    service.handle_write("dmx_prolet1", "red", "10")
    service.net.feed("sacn", sacn_packet(5, bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])), "10.0.0.1")
    assert shown(service, "dmx_prolet1", "red") == 1
    service.handle_write("dmx_logotip", "dimmer", "77")

    service.net.feed("sacn", sacn_packet(5, b"", options=SACN_OPT_TERMINATED), "10.0.0.1")

    assert shown(service, SERVICE_DEVICE, "rs485_2_source") == "MQTT"
    assert service.frame(PORT_PATH)[:9] == bytes([10, 0, 0, 0, 0, 0, 0, 0, 77])
    assert shown(service, "dmx_prolet1", "red") == 10
    assert shown(service, "dmx_prolet2", "white") == 0
    assert shown(service, "dmx_logotip", "dimmer") == 77, "what was written meanwhile is on the line now"


def test_disabled_port_is_listed_but_has_no_devices(tmp_path: Path) -> None:
    service = make_service([{"name": "Логотип", "layout": "d"}], tmp_path, enabled=False)

    assert shown(service, SERVICE_DEVICE, "rs485_2_state") == 0
    assert shown(service, SERVICE_DEVICE, "status") == 2
    assert "dmx_logotip" not in service.shown_values()
    assert service.failed_devices() == {SERVICE_DEVICE}, "nothing to do is not all right"


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
