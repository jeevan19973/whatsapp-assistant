"""Nursing skill — a live breastfeeding timer (start/complete) plus completed intervals.

Like sleep, the timer lives in Huckleberry (`start_nursing`/`complete_nursing`). PLAN.md §10
flags an upstream bug (#51) where a session started by the iOS app writes a millisecond
timestamp; that affects reads of app-started sessions, not timers we start, but we prefer
`log_nursing` with explicit start/end where the user gives them.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.parsers import common, timeparse
from app.parsers.common import MISSING, coerce_dt, resolve_llm_time
from app.skills.base import SkillResult
from app.skills.huckleberry.client import HuckleberryClient

log = logging.getLogger(__name__)

_NURSE_WORD = re.compile(r"\b(nursing|nursed|nurse|breastfed|breast-?feed(ing)?|latch(ed)?)\b", re.I)
_FEED_SIDE = re.compile(r"\b(feed(ing)?|fed)\b.*\b(left|right)\b|\b(left|right)\b.*\b(feed(ing)?|fed)\b", re.I)
_DONE = re.compile(r"\b(finished|done|stopped|ended|unlatch(ed)?|off the breast)\b", re.I)
_LEFT = re.compile(r"\bleft\b", re.I)
_RIGHT = re.compile(r"\bright\b", re.I)


def _has_two_times(text: str, tz: ZoneInfo) -> bool:
    first, remainder = timeparse.resolve(text, tz)
    if first is None:
        return False
    return timeparse.resolve(remainder, tz)[0] is not None


def _side_in(text: str) -> str | None:
    if _RIGHT.search(text):
        return "right"
    if _LEFT.search(text):
        return "left"
    return None


class NursingSkill:
    name = "huckleberry_nursing"

    tool_schema = {
        "name": "huckleberry_nursing",
        "description": (
            "Log breastfeeding/nursing to Huckleberry. Actions: 'start' when a feed is "
            "beginning now ('feeding now on the left'), 'complete' when it's just finished "
            "('done nursing'), or 'log' for a finished feed with start and end times. This is "
            "feeding at the breast — a bottle of expressed milk is a bottle, not nursing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "complete", "log"]},
                "side": {"type": "string", "enum": ["left", "right"], "description": "Which breast, if stated."},
                "start_time": {"type": "string", "description": "For action=log: 24-hour 'HH:MM' the feed began."},
                "end_time": {"type": "string", "description": "For action=log: 24-hour 'HH:MM' the feed ended."},
            },
            "required": ["action"],
        },
    }

    def __init__(self, client: HuckleberryClient, tz: ZoneInfo) -> None:
        self._client = client
        self._tz = tz

    # ---- parsing / clarification --------------------------------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        if not (_NURSE_WORD.search(text) or _FEED_SIDE.search(text)):
            return None
        if _has_two_times(text, tz):
            return None  # a range is a completed interval — hand to the LLM
        if _DONE.search(text):
            return {"action": "complete"}
        return {"action": "start", "side": _side_in(text) or "left"}

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        action = raw.get("action")
        side = raw.get("side") if raw.get("side") in ("left", "right") else "left"
        start_str = raw.get("start_time")
        end_str = raw.get("end_time")

        if action not in ("start", "complete", "log"):
            action = "log" if (start_str and end_str) else MISSING

        if action == "start":
            return {"action": "start", "side": side}
        if action == "complete":
            return {"action": "complete"}
        if action == MISSING:
            return {"action": MISSING}

        start = self._time_or_missing(start_str, tz)
        end = self._time_or_missing(end_str, tz)
        if isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
            end = end + timedelta(days=1)
        return {"action": "log", "side": side, "start_time": start, "end_time": end}

    @staticmethod
    def _time_or_missing(value: Any, tz: ZoneInfo) -> Any:
        if not isinstance(value, str) or not value.strip():
            return MISSING
        when, _ = timeparse.resolve(value, tz)
        return when if when is not None else MISSING

    def missing_question(self, args: dict[str, Any]) -> str | None:
        action = args.get("action")
        if action == MISSING:
            return "Is a feed starting now, just finished, or are you logging a past one with times?"
        if action == "log":
            if args.get("start_time") == MISSING:
                return "When did the feed start? e.g. `2pm`"
            if args.get("end_time") == MISSING:
                return "When did it end? e.g. `2:20pm`"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        args = dict(args)
        action = args.get("action")

        if action == MISSING:
            if _DONE.search(text):
                return {"action": "complete"}
            if _NURSE_WORD.search(text) or _side_in(text):
                return {"action": "start", "side": _side_in(text) or "left"}
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
            return await self._start(args.get("side", "left"))
        if action == "complete":
            return await self._complete()
        return await self._log(args)

    async def _start(self, side: str) -> SkillResult:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.start_nursing(child_uid, side=side)
        except Exception as exc:
            log.exception("start_nursing failed")
            return SkillResult(ok=False, message=f"Couldn't start a nursing timer — {type(exc).__name__}.", error=f"{type(exc).__name__}: {exc}")
        now = common.now_minute(self._tz)
        return SkillResult(ok=True, message=f"Nursing ({side}) started at {now:%H:%M}. Send `done` when finished.", verified=True)

    async def _complete(self) -> SkillResult:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.complete_nursing(child_uid)
        except Exception as exc:
            log.warning("complete_nursing failed (likely no timer running): %s", exc)
            return SkillResult(
                ok=True, verified=False,
                message="Couldn't stop a nursing timer — is one running? Start one with `feeding now`, or log a past feed like `nursed 2 to 2:20`.",
            )
        now = common.now_minute(self._tz)
        return SkillResult(ok=True, message=f"Nursing finished at {now:%H:%M}.", verified=True)

    async def _log(self, args: dict[str, Any]) -> SkillResult:
        start: datetime = args["start_time"]
        end: datetime = args["end_time"]
        side = args.get("side", "left")
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.log_nursing(child_uid, start_time=start, end_time=end, side=side)
        except Exception as exc:
            log.exception("log_nursing failed")
            return SkillResult(
                ok=False,
                message=(
                    f"Couldn't reach Huckleberry — {type(exc).__name__}. "
                    f"Saved locally as nursing {start:%H:%M}–{end:%H:%M}; retry with /retry."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

        mins = int((end - start).total_seconds() // 60)
        pretty = f"{start:%H:%M}–{end:%H:%M} ({mins}m, {side})"
        return SkillResult(ok=True, message=f"Nursing logged — {pretty}.", verified=False)
