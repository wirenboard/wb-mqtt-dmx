"""MQTT device ids derived from human names, so a user never has to type an id.

"Фасад" → "fasad", "Пролёт 1" → "prolet_1", "Logo (left)" → "logo_left". The id is lowercase latin
letters, digits and underscores, at most 32 characters, never empty. Duplicates inside one config get
a numeric suffix (fasad, fasad_2, …).
"""

from __future__ import annotations

import re

MAX_ID_LEN = 32
_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(text: str) -> str:
    return "".join(_RU.get(ch, ch) for ch in text.lower())


def id_from_name(name: str, fallback: str = "device") -> str:
    """A valid id for a name; `fallback` when nothing usable remains (e.g. a name of only symbols)."""
    raw = re.sub(r"[^a-z0-9]+", "_", transliterate(name)).strip("_")
    return (raw or fallback)[:MAX_ID_LEN].rstrip("_") or fallback


def unique_id(base: str, taken: set[str]) -> str:
    """`base` or base_2, base_3 … — the first one not in `taken`; the result is added to `taken`."""
    cid, n = base, 2
    while cid in taken:
        suffix = f"_{n}"
        cid, n = base[: MAX_ID_LEN - len(suffix)] + suffix, n + 1
    taken.add(cid)
    return cid
