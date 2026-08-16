"""Small parsing helpers shared across skills.

Kept deliberately tiny: time resolution (LLM hands back 'HH:MM' or 'now', the regex path
hands back spoken forms) and volume extraction, both used by more than one skill.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.parsers import timeparse

MISSING = "__missing__"

UNITS_WORDS = {
    "ml": "ml", "mls": "ml", "millilitres": "ml", "milliliters": "ml",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
}

_VOLUME = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(ml|mls|millilit(?:re|er)s?|oz|ounces?|fl\.?\s*oz)\b", re.I
)


def now_minute(tz: ZoneInfo) -> datetime:
    return datetime.now(tz).replace(second=0, microsecond=0)


def resolve_llm_time(time_str: Any, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Turn the LLM's time field ('11:00', 'now', or absent) into a concrete datetime.

    Returns (when, explicit). Falls back to now — never guesses a wrong past time.
    """
    if isinstance(time_str, str) and time_str.strip().lower() not in {"", "now"}:
        when, _ = timeparse.resolve(time_str, tz)
        if when is not None:
            return when, True
    return now_minute(tz), False


def coerce_dt(value: Any, tz: ZoneInfo) -> datetime:
    """Pending args round-trip through JSON, so a datetime comes back as a string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
        except ValueError:
            pass
    return now_minute(tz)


def parse_volume(text: str) -> tuple[float, str] | None:
    """Extract '(amount, units)' from a volume phrase, or None. Never guesses ml vs oz."""
    m = _VOLUME.search(text)
    if not m:
        return None
    amount = float(m.group(1))
    units = "oz" if "oz" in m.group(2).lower() or "ounce" in m.group(2).lower() else "ml"
    return amount, units


def entry_landed(entries: list[Any], start: datetime, tol_seconds: int = 60) -> bool:
    """Did a read-back interval land at ~`start`? Used to confirm a write, since every
    Huckleberry `log_*` returns None (PLAN.md §9) — a silent failure is otherwise invisible.
    """
    target = start.timestamp()
    for e in entries:
        try:
            if abs(float(e.start) - target) < tol_seconds:
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False
