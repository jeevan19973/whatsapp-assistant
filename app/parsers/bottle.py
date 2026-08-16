"""Deterministic bottle-feed parser — the regex fast path.

Token extraction rather than one monolithic pattern, so word order doesn't matter:
"11am 90ml breast milk" and "90ml breast milk at 11am" both work.

Deliberately conservative. Anything it isn't sure about returns a MISSING field so the router
can ask, rather than guessing — logging the wrong feed volume is worse than one extra question
(PLAN.md decision 4). In particular a bare number with no unit is never assumed to be ml,
because ml and oz differ ~30x.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.parsers.timeparse import resolve

# Maps to firebase_types.BottleType exactly. Longest keys first so "breast milk"
# is matched before a bare "milk" could be.
BOTTLE_TYPES: list[tuple[str, str]] = [
    (r"breast\s*milk", "Breast Milk"),
    (r"\bebm\b", "Breast Milk"),
    (r"\bbm\b", "Breast Milk"),
    (r"expressed", "Breast Milk"),
    (r"\bformula\b", "Formula"),
    (r"\bfm\b", "Formula"),
    (r"cow'?s?\s*milk", "Cow Milk"),
    (r"goat'?s?\s*milk", "Goat Milk"),
    (r"soy\s*milk", "Soy Milk"),
    (r"\bsoya?\b", "Soy Milk"),
    (r"tube\s*feed(ing)?", "Tube Feeding"),
]

_VOLUME = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(ml|mls|millilit(?:re|er)s?|oz|ounces?|fl\.?\s*oz)\b", re.I
)
_BARE_NUMBER = re.compile(r"\b(\d+(?:\.\d+)?)\b")
# "bottle" alone signals intent without naming contents
_BOTTLE_WORD = re.compile(r"\bbottle\b", re.I)

MISSING = "__missing__"


def _units(raw: str) -> str:
    return "oz" if "oz" in raw.lower() or "ounce" in raw.lower() else "ml"


def parse(text: str, tz: ZoneInfo, now: datetime | None = None) -> dict[str, Any] | None:
    """Return bottle args, or None if this text isn't a bottle entry at all.

    A returned dict may contain MISSING values — the caller must resolve those before
    dispatching to Huckleberry.
    """
    when, remainder = resolve(text, tz, now)

    bottle_type = None
    for pattern, canonical in BOTTLE_TYPES:
        if re.search(pattern, remainder, re.I):
            bottle_type = canonical
            remainder = re.sub(pattern, " ", remainder, flags=re.I)
            break

    volume_match = _VOLUME.search(remainder)
    has_bottle_word = bool(_BOTTLE_WORD.search(remainder))

    # Not a bottle entry unless there's a volume, a known milk type, or the word "bottle".
    if not volume_match and bottle_type is None and not has_bottle_word:
        return None

    if volume_match:
        amount: Any = float(volume_match.group(1))
        units = _units(volume_match.group(2))
    elif bare := _BARE_NUMBER.search(remainder):
        # A number with no unit. Refuse to guess ml vs oz.
        amount = float(bare.group(1))
        units = MISSING
    else:
        amount, units = MISSING, MISSING

    if amount != MISSING and float(amount) <= 0:
        return None

    return {
        "start_time": when or (now or datetime.now(tz)).replace(second=0, microsecond=0),
        "amount": amount,
        "units": units,
        "bottle_type": bottle_type or MISSING,
        "time_was_explicit": when is not None,
    }
