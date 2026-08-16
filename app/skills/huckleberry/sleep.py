"""Sleep skill — a live timer (start/complete) plus completed intervals.

The timer state lives in Huckleberry, not here: `start_sleep` begins it server-side and
`complete_sleep` ends it and logs the interval. Since there's one shared child, either
parent can start it and the other can end it. Completed intervals ('slept 2 to 4') go
through the LLM, which reasons about overnight ranges better than a regex can.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import re

from app.parsers import common, timeparse
from app.parsers.common import MISSING, coerce_dt, resolve_llm_time
from app.skills.base import SkillResult
from app.skills.huckleberry.client import HuckleberryClient

log = logging.getLogger(__name__)

_START = re.compile(r"\b(asleep|sleeping|went down|going down|put (her|him|them) down|down (for|now)|nap(ping)?( now)?|dozed off)\b", re.I)
_WAKE = re.compile(r"\b(awake|woke|woken|waking|got up|up now)\b", re.I)


def _has_two_times(text: str, tz: ZoneInfo) -> bool:
    first, remainder = timeparse.resolve(text, tz)
    if first is None:
        return False
    second, _ = timeparse.resolve(remainder, tz)
    return second is not None


def _fmt_duration(start: datetime, end: datetime) -> str:
    mins = int((end - start).total_seconds() // 60)
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h{m:02d}"
    return f"{h}h" if h else f"{m}m"


class SleepSkill:
    name = "huckleberry_sleep"

    tool_schema = {
        "name": "huckleberry_sleep",
        "description": (
            "Log baby sleep to Huckleberry. Three actions: 'start' when the baby has just "
            "gone to sleep now ('down now'), 'complete' when they've just woken up now "
            "('awake'), or 'log' for a finished past sleep given both a start and end time "
            "('slept 2pm to 4pm')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "complete", "log"],
                    "description": "start = asleep now; complete = awake now; log = a past interval.",
                },
                "start_time": {"type": "string", "description": "For action=log: 24-hour 'HH:MM' the sleep began."},
                "end_time": {"type": "string", "description": "For action=log: 24-hour 'HH:MM' the sleep ended."},
            },
            "required": ["action"],
        },
    }

    def __init__(self, client: HuckleberryClient, tz: ZoneInfo) -> None:
        self._client = client
        self._tz = tz

    # ---- parsing / clarification --------------------------------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        # A range like "2 to 4" is a completed interval — let the LLM handle overnight logic.
        if _has_two_times(text, tz):
            return None
        if _WAKE.search(text):
            return {"action": "complete"}
        if _START.search(text):
            return {"action": "start"}
        return None

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        action = raw.get("action")
        start_str = raw.get("start_time")
        end_str = raw.get("end_time")

        # Infer the action if the model gave times but no explicit action.
        if action not in ("start", "complete", "log"):
            action = "log" if (start_str and end_str) else MISSING

        if action != "log":
            return {"action": action}

        start = self._time_or_missing(start_str, tz)
        end = self._time_or_missing(end_str, tz)
        # Overnight: an end at or before the start means it ended the next day.
        if isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
            end = end + timedelta(days=1)
        return {"action": "log", "start_time": start, "end_time": end}

    @staticmethod
    def _time_or_missing(value: Any, tz: ZoneInfo) -> Any:
        if not isinstance(value, str) or not value.strip():
            return MISSING
        when, _ = timeparse.resolve(value, tz)
        return when if when is not None else MISSING

    def missing_question(self, args: dict[str, Any]) -> str | None:
        action = args.get("action")
        if action == MISSING:
            return "Did the baby just fall asleep, just wake up, or is this a past nap with times?"
        if action == "log":
            if args.get("start_time") == MISSING:
                return "When did the sleep start? e.g. `2pm`"
            if args.get("end_time") == MISSING:
                return "When did it end? e.g. `4pm`"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        args = dict(args)
        action = args.get("action")

        if action == MISSING:
            if _WAKE.search(text):
                return {"action": "complete"}
            if _START.search(text):
                return {"action": "start"}
            return None

        if action == "log":
            when, _ = timeparse.resolve(text, tz)
            if when is None:
                return None
            if args.get("start_time") == MISSING:
                args["start_time"] = when
                return args
            if args.get("end_time") == MISSING:
                start = coerce_dt(args.get("start_time"), tz)
                if when <= start:
                    when = when + timedelta(days=1)
                args["start_time"] = start
                args["end_time"] = when
                return args
        return None

    # ---- execution ----------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        action = args["action"]
        if action == "start":
            return await self._start()
        if action == "complete":
            return await self._complete()
        return await self._log(args)

    async def _start(self) -> SkillResult:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.start_sleep(child_uid)
        except Exception as exc:
            log.exception("start_sleep failed")
            return SkillResult(ok=False, message=f"Couldn't start a sleep timer — {type(exc).__name__}.", error=f"{type(exc).__name__}: {exc}")
        now = common.now_minute(self._tz)
        return SkillResult(ok=True, message=f"Sleep started at {now:%H:%M}. Send `awake` when they're up.", verified=True)

    async def _complete(self) -> SkillResult:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.complete_sleep(child_uid)
        except Exception as exc:
            log.warning("complete_sleep failed (likely no timer running): %s", exc)
            return SkillResult(
                ok=True, verified=False,
                message="Couldn't stop a sleep timer — is one running? Start one with `down now`, or log a past nap like `slept 2 to 4`.",
            )
        now = common.now_minute(self._tz)
        return SkillResult(ok=True, message=f"Awake — sleep logged, up at {now:%H:%M}.", verified=True)

    async def _log(self, args: dict[str, Any]) -> SkillResult:
        start: datetime = args["start_time"]
        end: datetime = args["end_time"]
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.log_sleep(child_uid, start_time=start, end_time=end)
        except Exception as exc:
            log.exception("log_sleep failed")
            return SkillResult(
                ok=False,
                message=(
                    f"Couldn't reach Huckleberry — {type(exc).__name__}. "
                    f"Saved locally as sleep {start:%H:%M}–{end:%H:%M}; retry with /retry."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

        verified = await self._verify(start)
        dur = _fmt_duration(start, end)
        pretty = f"{start:%H:%M}–{end:%H:%M} ({dur})"
        if verified:
            return SkillResult(ok=True, message=f"Sleep logged — {pretty}.", verified=True)
        return SkillResult(ok=True, message=f"Sleep sent — {pretty}. (Couldn't confirm it back; check the app.)", verified=False)

    async def _verify(self, start: datetime) -> bool:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            now = datetime.now(start.tzinfo)
            entries = await api.list_sleep_intervals(
                child_uid,
                start_time=min(start, now) - timedelta(minutes=5),
                end_time=now + timedelta(minutes=5),
            )
        except Exception:
            log.warning("sleep verification read failed", exc_info=True)
            return False
        return common.entry_landed(entries, start)
