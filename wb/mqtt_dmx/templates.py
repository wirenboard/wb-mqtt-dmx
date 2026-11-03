"""Decoder templates: which DMX channels a device type has and in which order.

A template is a small JSON file:

    {"type": "rgbw", "title": {"en": "RGBW (4 ch)", "ru": "RGBW (4 канала)"},
     "channels": [{"id": "red", "title": {"en": "Red", "ru": "Красный"}, "default": 0}, ...]}

Channel order is DMX order; a channel may carry `"offset": N` (0-based) to skip unused addresses,
otherwise channels are consecutive. The package ships a base set in /usr/share/wb-mqtt-dmx/templates;
a user adds their own in /etc/wb-mqtt-dmx.conf.d/templates (same file name overrides the base one),
the way wb-mqtt-serial templates work.
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


class TemplateError(ValueError):
    """A template file is malformed."""


@dataclass
class Channel:
    id: str
    title: dict[str, str]
    offset: int                      # 0-based offset from the device start address
    default: int = 0


@dataclass
class Template:
    type: str
    title: dict[str, str]
    channels: list[Channel] = field(default_factory=list)
    source: str = ""                 # file the template came from, for messages

    @property
    def footprint(self) -> int:
        """DMX addresses the device occupies."""
        return max((c.offset + 1 for c in self.channels), default=0)


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
        if not 0 <= offset < MAX_CHANNELS:
            raise TemplateError(f"{where}: offset out of range")
        default = int(c.get("default", 0))
        if not 0 <= default <= 255:
            raise TemplateError(f"{where}: default must be 0..255")
        channels.append(Channel(cid, _title(c.get("title"), cid, where), offset, default))
        next_offset = offset + 1
    return Template(key, _title(doc.get("title"), key, source), channels, source)


def load_templates(dirs: list[Path] | None = None) -> dict[str, Template]:
    """type -> Template from the base and user directories; later directories override."""
    out: dict[str, Template] = {}
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
