"""Device types: which DMX channels a device has, in which order, and what they mean.

Three sources, all producing the same `Template`:

* **Layout string** (the default type, like Home Assistant's `channel_setup`): one letter per channel,
  `d` dimmer, `r` `g` `b` red/green/blue, `w` white, `a` amber, `u` UV, `c` cold white, `h` warm white,
  `s` strobe, `m` mode (the program / function channel of a fixture, a plain 0–255 value), `_` an unused
  channel. `dRGBW` is a 5-channel decoder with a master dimmer. Case does not matter. With 16-bit
  resolution every channel takes two DMX slots (coarse, fine). A modes string next to the layout names the
  ranges of the `m` channel (`0 Manual, 51 Jump, 101 Fade`), which turns it into a drop-down list.
* **Channels**: N plain channels ch1…chN, for anything else.
* **JSON template files** for devices with named channels, modes and enum channels (strobe programs,
  wheels); the package may ship some, a user adds their own in /etc/wb-mqtt-dmx.conf.d/templates.
  Also the target format of a future OFL / GDTF converter. A template file:

    {"type": "my-par", "title": {"en": "My PAR", "ru": "Мой PAR"},
     "channels": [{"id": "red", "title": {"en": "Red", "ru": "Красный"}, "default": 0, "bits": 8}, …]}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path("/usr/share/wb-mqtt-dmx/templates")
USER_DIR = Path("/etc/wb-mqtt-dmx.conf.d/templates")
ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_CHANNELS = 512

LAYOUT_TYPE = "layout"
LAYOUT_TITLE = {"en": "Layout", "ru": "Раскладка"}
CHANNELS_TYPE = "channels"
CHANNELS_TITLE = {"en": "Channels", "ru": "Каналы"}
BUILTIN_TYPES = (LAYOUT_TYPE, CHANNELS_TYPE)
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
    """A template file or a layout string is malformed."""


@dataclass
class Range:
    """A named span of a channel's values: what a fixture does while the channel is within it."""
    start: int
    end: int
    title: dict[str, str]

    @property
    def value(self) -> int:
        """What is sent for this range: its middle, clear of both borders."""
        return (self.start + self.end) // 2


# "0 Manual, 51 Jump" — also "0-50 Manual; 51-100: Jump", the way manuals print it
MODES_RE = r"^\s*(\d{1,3}\s*(-\s*\d{1,3}\s*)?[:=]?\s*[^\s,;\d][^,;]*([,;]\s*|$))*$"
_MODE_ITEM = re.compile(r"^(\d{1,3})\s*(?:-\s*(\d{1,3})\s*)?[:=]?\s*(\S.*)$")
MAX_MODES = 32


def parse_modes(text: str) -> list[Range]:
    """Ranges from a modes string; each runs up to the start of the next one, the last to 255."""
    starts: list[tuple[int, str]] = []
    for item in re.split(r"[,;]", str(text or "")):
        item = item.strip()
        if not item:
            continue
        m = _MODE_ITEM.match(item)
        if not m:
            raise TemplateError(f"modes: {item!r} is not 'value name', e.g. 0 Manual, 51 Jump")
        start, end, name = int(m.group(1)), m.group(2), m.group(3).strip()
        if start > 255 or (end is not None and not start <= int(end) <= 255):
            raise TemplateError(f"modes: {item!r} is out of the range 0..255")
        if starts and start <= starts[-1][0]:
            raise TemplateError(f"modes: {item!r} must start after {starts[-1][0]}")
        starts.append((start, name[:32]))
    if not starts:
        return []
    if starts[0][0] != 0:
        raise TemplateError("modes: the first mode must start at 0")
    if len(starts) > MAX_MODES:
        raise TemplateError(f"modes: at most {MAX_MODES} modes")
    ends = [s - 1 for s, _ in starts[1:]] + [255]
    return [Range(s, e, {"en": name, "ru": name}) for (s, name), e in zip(starts, ends)]


@dataclass
class Channel:
    id: str
    title: dict[str, str]
    offset: int                      # 0-based offset of the coarse byte from the device start address
    default: int = 0
    bits: int = 8                    # 16: the fine byte follows the coarse one
    ranges: list[Range] = field(default_factory=list)   # named ranges: the control is a drop-down list

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
    source: str = ""                 # file the template came from, for messages
    dynamic: bool = False            # built in; shape comes from the device entry
    size: int = 0                    # addresses taken when unused channels follow the last named one

    @property
    def footprint(self) -> int:
        """DMX addresses the device occupies."""
        return max(self.size, max((c.offset + c.width for c in self.channels), default=0))


def _title(raw: Any, fallback: str, where: str) -> dict[str, str]:
    if isinstance(raw, str):
        return {"en": raw, "ru": raw}
    if isinstance(raw, dict) and raw.get("en"):
        return {"en": str(raw["en"]), "ru": str(raw.get("ru", raw["en"]))}
    if raw is None:
        return {"en": fallback, "ru": fallback}
    raise TemplateError(f"{where}: title must be a string or {{\"en\": …, \"ru\": …}}")


def parse_template(doc: Any, source: str = "") -> Template:
    if not isinstance(doc, dict):
        raise TemplateError(f"{source}: not a JSON object")
    key = str(doc.get("type") or "")
    if not ID_RE.match(key):
        raise TemplateError(f"{source}: type must match {ID_RE.pattern}, got {key!r}")
    if key in BUILTIN_TYPES:
        raise TemplateError(f"{source}: the type '{key}' is built in")
    chans_raw = doc.get("channels")
    if not isinstance(chans_raw, list) or not chans_raw:
        raise TemplateError(f"{source}: channels must be a non-empty list")
    channels: list[Channel] = []
    seen: set[str] = set()
    next_offset = 0
    for n, c in enumerate(chans_raw, start=1):
        where = f"{source}: channel {n}"
        if not isinstance(c, dict):
            raise TemplateError(f"{where}: must be an object")
        cid = str(c.get("id") or "")
        if not ID_RE.match(cid):
            raise TemplateError(f"{where}: id must match {ID_RE.pattern}, got {cid!r}")
        if cid in seen:
            raise TemplateError(f"{where}: duplicate id {cid!r}")
        seen.add(cid)
        offset = int(c.get("offset", next_offset))
        bits = int(c.get("bits", 8))
        if bits not in (8, 16):
            raise TemplateError(f"{where}: bits must be 8 or 16")
        if not 0 <= offset < MAX_CHANNELS:
            raise TemplateError(f"{where}: offset out of range")
        default = int(c.get("default", 0))
        if not 0 <= default <= (65535 if bits == 16 else 255):
            raise TemplateError(f"{where}: default out of range")
        channels.append(Channel(cid, _title(c.get("title"), cid, where), offset, default, bits))
        next_offset = offset + (2 if bits == 16 else 1)
    return Template(key, _title(doc.get("title"), key, source), channels, source)


def layout_template(layout: str, bits: int = 8, modes: str = "") -> Template:
    """A device described by a layout string, e.g. 'dRGBW' or 'rgb' (see the module docstring)."""
    layout = str(layout or "").strip()
    # Modes left over after the m was removed from the layout are not an error: nothing to apply them to
    ranges = parse_modes(modes) if "m" in layout.lower() else []
    if ranges and bits != 8:
        raise TemplateError("modes need the 8-bit resolution")
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
            channels.append(Channel(cid, title, offset, 0, bits, ranges if letter == "m" else []))
        offset += width
    if not channels:
        raise TemplateError(f"layout {layout!r}: at least one channel must not be '_'")
    return Template(LAYOUT_TYPE, LAYOUT_TITLE, channels, "layout", dynamic=True, size=offset)


def channels_template(count: int) -> Template:
    """N plain channels ch1…chN."""
    chans = [Channel(f"ch{i}", {"en": f"Ch {i}", "ru": f"Канал {i}"}, i - 1) for i in range(1, count + 1)]
    return Template(CHANNELS_TYPE, CHANNELS_TITLE, chans, "built-in", dynamic=True)


def builtin_templates() -> dict[str, Template]:
    return {LAYOUT_TYPE: layout_template(DEFAULT_LAYOUT), CHANNELS_TYPE: channels_template(1)}


def load_templates(dirs: list[Path] | None = None) -> dict[str, Template]:
    """type -> Template: the built-in types plus every file of the directories (later ones override)."""
    out = builtin_templates()
    for d in dirs if dirs is not None else [BASE_DIR, USER_DIR]:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise TemplateError(f"{path}: {e}") from e
            t = parse_template(doc, str(path))
            out[t.type] = t
    return out
