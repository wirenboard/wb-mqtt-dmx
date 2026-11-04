"""MQTT controls of a device: one `range` control per template channel (0..255, or 0..65535 for a
16-bit channel whose fine byte follows the coarse one)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .templates import Channel, Template


@dataclass
class Control:
    id: str
    title: dict[str, str]
    channel: Channel

    @property
    def max(self) -> int:
        return self.channel.max_value

    def meta(self, order: int) -> dict[str, Any]:
        return {"type": "range", "order": order, "readonly": False, "min": 0, "max": self.max, "title": self.title}

    @property
    def default(self) -> int:
        return self.channel.default

    @property
    def zero(self) -> int:
        return 0

    def parse(self, payload: str) -> int | None:
        """The stored value of an MQTT write, or None when the payload is not a number."""
        try:
            return max(0, min(self.max, int(float(payload))))
        except ValueError:
            return None

    def value_from(self, read: dict[int, int]) -> int:
        """Rebuild the value from slot bytes (offset -> byte), e.g. after a state restore."""
        coarse = read.get(self.channel.offset, 0)
        if self.channel.bits == 16:
            return (coarse << 8) | read.get(self.channel.offset + 1, 0)
        return coarse


def build_controls(template: Template) -> list[Control]:
    return [Control(c.id, c.title, c) for c in template.channels]


def render(controls: list[Control], values: dict[str, Any], footprint: int) -> bytes:
    out = bytearray(footprint)
    for control in controls:
        value, ch = int(values[control.id]), control.channel
        if ch.bits == 16:
            out[ch.offset], out[ch.offset + 1] = (value >> 8) & 0xFF, value & 0xFF
        else:
            out[ch.offset] = value & 0xFF
    return bytes(out)
