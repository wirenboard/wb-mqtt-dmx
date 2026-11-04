"""MQTT controls of a device and how they map onto its DMX channels.

A template channel becomes a `range` control (0..255, or 0..65535 for a 16-bit channel). When the
template names an `rgb` triple, those three channels fold into one `rgb` control (`"R;G;B"`, 8-bit
components), which the web UI shows as a colour palette instead of three sliders; on 16-bit channels
each component is scaled to the full width.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .templates import Channel, Template

RGB_CONTROL = "color"
RGB_TITLE = {"en": "Color", "ru": "Цвет"}


@dataclass
class Control:
    id: str
    type: str                        # range | rgb
    title: dict[str, str]
    channels: list[Channel]          # one channel, or R, G, B in order

    @property
    def max(self) -> int:
        return 255 if self.type == "rgb" else self.channels[0].max_value

    def meta(self, order: int) -> dict[str, Any]:
        meta: dict[str, Any] = {"type": self.type, "order": order, "readonly": False, "title": self.title}
        if self.type == "range":
            meta["min"], meta["max"] = 0, self.max
        return meta

    @property
    def default(self) -> Any:
        if self.type == "rgb":
            return ";".join(str(_to8(c.default, c)) for c in self.channels)
        return self.channels[0].default

    @property
    def zero(self) -> Any:
        return "0;0;0" if self.type == "rgb" else 0

    def parse(self, payload: str) -> Any | None:
        """The stored value of an MQTT write, or None when the payload is not acceptable."""
        try:
            if self.type == "rgb":
                parts = [max(0, min(255, int(float(p)))) for p in payload.split(";")]
                if len(parts) != 3:
                    return None
                return ";".join(str(p) for p in parts)
            return max(0, min(self.max, int(float(payload))))
        except ValueError:
            return None

    def channel_values(self, value: Any) -> list[int]:
        """Full-width channel values in self.channels order."""
        if self.type == "rgb":
            return [_from8(int(p), c) for p, c in zip(str(value).split(";"), self.channels)]
        return [int(value)]

    def value_from(self, read: dict[int, int]) -> Any:
        """Rebuild the value from slot bytes (offset -> byte), e.g. after a state restore."""
        raw = [_read_channel(read, c) for c in self.channels]
        if self.type == "rgb":
            return ";".join(str(_to8(v, c)) for v, c in zip(raw, self.channels))
        return raw[0]


def _to8(value: int, channel: Channel) -> int:
    return value >> 8 if channel.bits == 16 else value


def _from8(value: int, channel: Channel) -> int:
    return value * 257 if channel.bits == 16 else value


def _read_channel(read: dict[int, int], channel: Channel) -> int:
    coarse = read.get(channel.offset, 0)
    return (coarse << 8) | read.get(channel.offset + 1, 0) if channel.bits == 16 else coarse


def build_controls(template: Template) -> list[Control]:
    by_id = {c.id: c for c in template.channels}
    folded: set[str] = set()
    controls: list[Control] = []
    if template.rgb and all(cid in by_id for cid in template.rgb):
        folded.update(template.rgb)
        controls.append(Control(RGB_CONTROL, "rgb", RGB_TITLE, [by_id[cid] for cid in template.rgb]))
    for c in template.channels:
        if c.id not in folded:
            controls.append(Control(c.id, "range", c.title, [c]))
    return controls


def render(controls: list[Control], values: dict[str, Any], footprint: int) -> bytes:
    out = bytearray(footprint)
    for control in controls:
        for channel, value in zip(control.channels, control.channel_values(values[control.id])):
            if channel.bits == 16:
                out[channel.offset], out[channel.offset + 1] = (value >> 8) & 0xFF, value & 0xFF
            else:
                out[channel.offset] = value & 0xFF
    return bytes(out)
