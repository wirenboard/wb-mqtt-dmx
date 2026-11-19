import pytest

from wb.mqtt_dmx import templates
from wb.mqtt_dmx.controls import ControlSpec, build_controls, render


def test_layout_gives_one_channel_per_letter() -> None:
    template = templates.layout_template("dRGBW")

    assert [(c.id, c.offset) for c in template.channels] == [
        ("dimmer", 0),
        ("red", 1),
        ("green", 2),
        ("blue", 3),
        ("white", 4),
    ]
    assert template.footprint == 5
    assert template.title["ru"] == "Раскладка"


def test_16_bit_layout_takes_two_addresses_per_channel() -> None:
    template = templates.layout_template("ch", 16)

    assert [(c.id, c.offset, c.bits) for c in template.channels] == [("cold", 0, 16), ("warm", 2, 16)]
    assert template.footprint == 4


def test_unused_channels_take_addresses_also_at_the_end() -> None:
    assert [c.offset for c in templates.layout_template("r_g_b").channels] == [0, 2, 4]
    assert templates.layout_template("r_g_b").footprint == 5
    assert templates.layout_template("drgb___").footprint == 7
    assert templates.layout_template("d_", 16).footprint == 4


def test_repeated_letter_gets_a_number() -> None:
    twice = templates.layout_template("ww")

    assert [c.id for c in twice.channels] == ["white", "white_2"]
    assert twice.channels[1].title["ru"] == "Белый 2"


def test_mode_letter_is_a_plain_channel() -> None:
    par = templates.layout_template("drgbwms")

    assert [c.id for c in par.channels][-2:] == ["mode", "strobe"]
    assert par.channels[5].title == {"en": "Mode", "ru": "Режим"}


@pytest.mark.parametrize("layout", ["", "xyz", "___", "r" * 65])
def test_bad_layout_is_rejected(layout: str) -> None:
    with pytest.raises(templates.TemplateError):
        templates.layout_template(layout)


def test_bits_other_than_8_and_16_are_rejected() -> None:
    with pytest.raises(templates.TemplateError, match="bits must be 8 or 16"):
        templates.layout_template("d", 12)


def test_channels_type_gives_plain_numbered_channels() -> None:
    template = templates.channels_template(3)

    assert [(c.id, c.offset) for c in template.channels] == [("ch1", 0), ("ch2", 1), ("ch3", 2)]
    assert template.footprint == 3
    assert template.title["ru"] == "Каналы"


def test_control_is_a_slider_over_the_channel_range() -> None:
    dimmer, white = build_controls(templates.layout_template("dw", 16))

    assert dimmer.spec() == ControlSpec(
        "dimmer", "range", {"en": "Dimmer", "ru": "Диммер"}, readonly=False, maximum=65535
    )
    assert white.spec().id == "white"


@pytest.mark.parametrize(
    "payload, value", [("0", 0), ("200", 200), ("999", 255), ("-3", 0), ("12.7", 12), ("x", None)]
)
def test_written_value_is_clamped_to_the_channel(payload: str, value: object) -> None:
    control = build_controls(templates.layout_template("d"))[0]

    assert control.parse(payload) == value


def test_values_render_into_slots_and_read_back() -> None:
    controls = build_controls(templates.layout_template("d_w", 16))
    data = render(controls, {"dimmer": 0x1234, "white": 0xABCD}, 6)

    assert data == bytes([0x12, 0x34, 0, 0, 0xAB, 0xCD]), "coarse byte first, the unused channel stays 0"
    read = dict(enumerate(data))
    assert [c.value_from(read) for c in controls] == [0x1234, 0xABCD]
