import json
from pathlib import Path
from typing import Any

import pytest

from wb.mqtt_dmx.config import ConfigError, Port, address_map, load_config

from .fakes import port_entry

ROOT = Path(__file__).parent.parent


def dev(name: str, type_: str = "layout", layout: str = "rgbw", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": type_, "layout": layout, **extra}


def one_port(*devices: dict[str, Any], **settings: Any) -> dict[str, Any]:
    return {"ports": [port_entry(list(devices), **settings)]}


def only_port(config: dict[str, Any]) -> Port:
    ports = load_config(config)
    assert len(ports) == 1
    return ports[0]


def test_shipped_config_has_two_disabled_ports() -> None:
    config = json.loads((ROOT / "wb-mqtt-dmx.conf").read_text(encoding="utf-8"))

    assert [(p.path, p.enabled, p.key) for p in load_config(config)] == [
        ("/dev/ttyRS485-1", False, "rs485_1"),
        ("/dev/ttyRS485-2", False, "rs485_2"),
    ]


def test_addresses_follow_each_other_unless_set() -> None:
    port = only_port(
        one_port(dev("Фасад", count=12), dev("Логотип", layout="d"), dev("Вывеска", layout="ch", address=100))
    )

    assert [(d.id, d.address) for d in port.devices][:2] == [("fasad1", 1), ("fasad2", 5)]
    assert [(d.id, d.name, d.address) for d in port.devices][12:] == [
        ("logotip", "Логотип", 49),
        ("vyveska", "Вывеска", 100),
    ]
    assert address_map(port) == "Фасад: 1-48 (12 × 4); Логотип: 49; Вывеска: 100-101"


def test_count_above_one_makes_a_group_of_members() -> None:
    port = only_port(one_port(dev("Фасад", count=12)))
    group = port.groups[0]

    assert group.device_id == "dmx_fasad"
    assert (group.address, group.last_address) == (1, 48)
    assert all(member.group == "fasad" for member in group.members)


def test_next_device_starts_after_the_unused_channels_of_the_previous_one() -> None:
    port = only_port(one_port(dev("Пар", layout="drgb___", count=2), dev("Следом", layout="d")))

    assert [(d.id, d.address) for d in port.devices] == [("par1", 1), ("par2", 8), ("sledom", 15)]


def test_channels_type_takes_its_count_from_the_entry() -> None:
    port = only_port(one_port(dev("Дым", "channels", channels=3), dev("Строб", "channels"), dev("A")))

    assert [(d.id, d.address, d.template.footprint) for d in port.devices] == [
        ("dym", 1, 3),
        ("strob", 4, 1),
        ("a", 5, 4),
    ]


def test_16_bit_device_takes_two_addresses_per_channel() -> None:
    port = only_port(one_port(dev("Пар", layout="dRGBW"), dev("Диммер", layout="d", bits=16)))

    assert [(d.id, d.address, d.template.footprint) for d in port.devices] == [
        ("par", 1, 5),
        ("dimmer", 6, 2),
    ]


def test_pinned_id_is_used_as_is() -> None:
    port = only_port(one_port(dev("Logo", id="logo_main"), dev("Лого", layout="d")))

    assert [d.id for d in port.devices] == ["logo_main", "logo"]


def test_derived_ids_that_collide_get_a_suffix() -> None:
    port = only_port(one_port(dev("Logo"), dev("Logo!", layout="d")))

    assert [d.id for d in port.devices] == ["logo", "logo_2"]


def test_enabled_is_read_from_the_port_itself() -> None:
    config = {"ports": [port_entry([], enabled=True), port_entry([], path="/dev/ttyRS485-1", enabled=False)]}

    assert [p.enabled for p in load_config(config)] == [True, False]


def test_network_universes_default_to_the_port_position() -> None:
    config = {
        "ports": [
            port_entry([], network={"artnet": True, "sacn": True}),
            port_entry([], path="/dev/ttyRS485-1", network={"artnet": True, "artnet_universe": 7}),
        ]
    }
    ports = load_config(config)
    first, second = ports[0], ports[1]

    assert (first.artnet_universe, first.sacn_universe) == (0, 1)
    assert (second.artnet_universe, second.sacn_universe) == (7, None)


def test_pause_between_channels_is_off_by_default() -> None:
    plain = only_port(one_port())
    slow = only_port(one_port(advanced={"slot_gap_us": 50}))

    assert (plain.slot_gap_us, slow.slot_gap_us) == (0, 50)


@pytest.mark.parametrize(
    "config, message",
    [
        ({}, "at least one port"),
        (one_port(dev("Logo"), dev("Other", id="logo")), "id 'logo' is already used"),
        (one_port(dev("A", address=1), dev("B", address=3)), "overlap"),
        (one_port(dev("A", count=129)), "past the 512 channels"),
        (one_port(dev("A", "nope")), "unknown type 'nope'"),
        (one_port(dev("A", layout="")), "letters d r g b"),
        (one_port(dev("A", layout="q")), "letters d r g b"),
        (one_port(dev("A", bits=12)), "bits must be 8 or 16"),
        (one_port(dev("")), "has no name"),
        (one_port(refresh_hz=60), "refresh_hz must be 1..44"),
        (one_port(on_stop="explode"), "on_stop must be hold or blackout"),
        (one_port(advanced={"slot_gap_us": 5000}), "slot_gap_us must be 0..1000"),
        (one_port(advanced="fast"), "advanced must be an object"),
        ({"ports": [port_entry([]), port_entry([])]}, "listed twice"),
        (
            {"ports": [port_entry([dev("Фасад")], path="/dev/ttyRS485-1"), port_entry([dev("фасад")])]},
            "name 'фасад' is used twice",
        ),
        (
            {
                "ports": [
                    port_entry([], path="/dev/ttyRS485-1", network={"artnet": True, "artnet_universe": 3}),
                    port_entry([], network={"artnet": True, "artnet_universe": 3}),
                ]
            },
            "both receive Art-Net universe 3",
        ),
    ],
)
def test_unusable_config_is_refused_with_a_message(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(config)
