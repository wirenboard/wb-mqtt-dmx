"""MQTT controls of a device and how they map onto its DMX channels.

A template channel becomes a `range` control (0..255). When the template names an `rgb` triple,
those three channels fold into one `rgb` control (`"R;G;B"`), which the web UI shows as a colour
palette instead of three sliders.
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

    def meta(self, order: int) -> dict[str, Any]:
        meta: dict[str, Any] = {"type": self.type, "order": order, "readonly": False, "title": self.title}
        if self.type == "range":
            meta["min"], meta["max"] = 0, 255
        return meta

    @property
    def default(self) -> Any:
        if self.type == "rgb":
            return ";".join(str(c.default) for c in self.channels)
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
            return max(0, min(255, int(float(payload))))
        except ValueError:
            return None

    def bytes_of(self, value: Any) -> list[int]:
        """Channel values in self.channels order."""
        if self.type == "rgb":
            return [int(p) for p in str(value).split(";")]
        return [int(value)]

    def value_from(self, read: dict[int, int]) -> Any:
        """Rebuild the value from channel values (offset -> byte), e.g. after a state restore."""
        parts = [read.get(c.offset, 0) for c in self.channels]
        return ";".join(str(p) for p in parts) if self.type == "rgb" else parts[0]


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
        for channel, byte in zip(control.channels, control.bytes_of(values[control.id])):
            out[channel.offset] = byte
    return bytes(out)
