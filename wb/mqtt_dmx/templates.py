"""
Device types: which DMX channels a device has, in which order, and what they mean.

Two built-in types, both producing a `Template`:

* **Layout string** (the default type, like Home Assistant's `channel_setup`): one letter per channel,
  `d` dimmer, `r` `g` `b` red/green/blue, `w` white, `a` amber, `u` UV, `c` cold white, `h` warm white,
  `s` strobe, `m` mode (the program / function channel of a fixture, a plain 0–255 value), `_` an unused
  channel. `dRGBW` is a 5-channel decoder with a master dimmer. Case does not matter. With 16-bit
  resolution every channel takes two DMX slots (coarse, fine).
* **Channels**: N plain channels ch1…chN, for anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LAYOUT_TYPE = "layout"
LAYOUT_TITLE = {"en": "Layout", "ru": "Раскладка"}
CHANNELS_TYPE = "channels"
CHANNELS_TITLE = {"en": "Channels", "ru": "Каналы"}
DEVICE_TYPES = (LAYOUT_TYPE, CHANNELS_TYPE)
DEFAULT_LAYOUT = "rgbw"
LAYOUT_RE = r"^[dDrRgGbBwWaAuUcChHsSmM_]{1,64}$"

# letter -> (control id, titles); the same letter twice gets _2, _3 …
LAYOUT_LETTERS = {
    "d": ("dimmer", {"en": "Dimmer", "ru": "Диммер"}),
    "r": ("red", {"en": "Red", "ru": "Красный"}),
    "g": ("green", {"en": "Green", "ru": "Зелёный"}),
    "b": ("blue", {"en": "Blue", "ru": "Синий"}),
    "w": ("white", {"en": "White", "ru": "Белый"}),
    "a": ("amber", {"en": "Amber", "ru": "Янтарный"}),
    "u": ("uv", {"en": "UV", "ru": "УФ"}),
    "c": ("cold", {"en": "Cold white", "ru": "Холодный белый"}),
    "h": ("warm", {"en": "Warm white", "ru": "Тёплый белый"}),
    "s": ("strobe", {"en": "Strobe", "ru": "Строб"}),
    "m": ("mode", {"en": "Mode", "ru": "Режим"}),
}


class TemplateError(ValueError):
    """
    A layout string is malformed.
    """


@dataclass
class Channel:
    id: str
    title: dict[str, str]
    offset: int  # 0-based offset of the coarse byte from the device start address
    default: int = 0
    bits: int = 8  # 16: the fine byte follows the coarse one

    @property
    def width(self) -> int:
        return 2 if self.bits == 16 else 1

    @property
    def max_value(self) -> int:
        return 65535 if self.bits == 16 else 255


@dataclass
class Template:
    type: str
    title: dict[str, str]
    channels: list[Channel] = field(default_factory=list)
    size: int = 0  # addresses taken when unused channels follow the last named one

    @property
    def footprint(self) -> int:
        """
        DMX addresses the device occupies.
        """
        return max(self.size, max((c.offset + c.width for c in self.channels), default=0))


def layout_template(layout: str, bits: int = 8) -> Template:
    """
    A device described by a layout string, e.g. 'dRGBW' or 'rgb' (see the module docstring).
    """
    layout = str(layout or "").strip()
    if not re.match(LAYOUT_RE, layout):
        raise TemplateError(f"layout {layout!r}: use the letters d r g b w a u c h s m and _ (1..64 of them)")
    if bits not in (8, 16):
        raise TemplateError("bits must be 8 or 16")
    channels: list[Channel] = []
    counts: dict[str, int] = {}
    offset = 0
    width = 2 if bits == 16 else 1
    for letter in layout.lower():
        if letter != "_":
            cid, title = LAYOUT_LETTERS[letter]
            counts[cid] = counts.get(cid, 0) + 1
            if counts[cid] > 1:
                cid, title = f"{cid}_{counts[cid]}", {k: f"{v} {counts[cid]}" for k, v in title.items()}
            channels.append(Channel(cid, title, offset, 0, bits))
        offset += width
    if not channels:
        raise TemplateError(f"layout {layout!r}: at least one channel must not be '_'")
    return Template(LAYOUT_TYPE, LAYOUT_TITLE, channels, size=offset)


def channels_template(count: int) -> Template:
    """
    N plain channels ch1…chN.
    """
    chans = [Channel(f"ch{i}", {"en": f"Ch {i}", "ru": f"Канал {i}"}, i - 1) for i in range(1, count + 1)]
    return Template(CHANNELS_TYPE, CHANNELS_TITLE, chans)
