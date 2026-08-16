"""Resolve a spoken time reference against a local timezone.

Rule from PLAN.md decision 7: if the resolved time lands in the future, it meant yesterday.
"11am" typed at 09:00 means 11am today; typed at 13:00 it still means 11am today; but
"11pm" typed at 00:30 means last night.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# 2:30pm / 14:00 / 2.30 pm
_HHMM = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", re.I)
# 11am / 11 pm
_HH_MERIDIEM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.I)
_NOW = re.compile(r"\b(now|just now|right now)\b", re.I)


def _apply_meridiem(hour: int, meridiem: str | None) -> int | None:
    if meridiem is None:
        return hour if 0 <= hour <= 23 else None
    m = meridiem.lower()
    if not 1 <= hour <= 12:
        return None
    if m == "am":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def resolve(text: str, tz: ZoneInfo, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Return (resolved datetime or None, text with the time token removed)."""
    now = now or datetime.now(tz)

    if match := _NOW.search(text):
        return now.replace(second=0, microsecond=0), _strip(text, match)

    for pattern, has_minutes in ((_HHMM, True), (_HH_MERIDIEM, False)):
        for match in pattern.finditer(text):
            hour_raw = int(match.group(1))
            minute = int(match.group(2)) if has_minutes else 0
            meridiem = match.group(3) if has_minutes else match.group(2)
            if minute > 59:
                continue
            hour = _apply_meridiem(hour_raw, meridiem)
            if hour is None:
                continue
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                candidate -= timedelta(days=1)
            return candidate, _strip(text, match)

    return None, text


def _strip(text: str, match: re.Match) -> str:
    return (text[: match.start()] + " " + text[match.end() :]).strip()
