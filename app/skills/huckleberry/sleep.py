"""Sleep skill — a live timer (start/complete) plus completed intervals.

The timer state lives in Huckleberry, not here: `start_sleep` begins it server-side and
`complete_sleep` ends it and logs the interval. Since there's one shared child, either
parent can start it and the other can end it. Completed intervals ('slept 2 to 4') go
through the LLM, which reasons about overnight ranges better than a regex can.

Either end may be backdated ('sleep start 5:40am' sent at 08:30, 'awake at 7:30' sent at
08:00). That matters at the start because otherwise an in-progress sleep with a known start
time has nowhere to land, the only fit is `log` with no end, and answering the resulting
"when did it end?" with "she's still sleeping" used to drop the 5:40 and open a fresh timer
at 08:30. It matters at the end because you notice the baby is up before you get to your
phone, and the timer would otherwise absorb the gap.
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

_START = re.compile(
    r"\b(asleep|sleeping|went down|going down|put (her|him|them) down|down (for|now)|"
    r"nap(ping)?( now)?|dozed off|(went|going) to sleep|sleep start(ed|ing)?|start(ed|ing)? sleep)\b",
    re.I,
)
_WAKE = re.compile(r"\b(awake|woke|woken|waking|got up|up now)\b", re.I)
# An answer to "when did it end?" that means it hasn't: the sleep is still running.
_NOT_ENDED = re.compile(
    r"\b(still (asleep|sleeping|sleep|down|napping|going|out)|not yet|no end|ongoing|"
    r"hasn'?t (woken( up)?|got up)|not awake( yet)?)\b",
    re.I,
)
# A bare negative reply to the same question. Matched whole-string only: 'no' inside a
# longer sentence ('no idea, maybe 4pm') is not a claim that the sleep is still running.
_BARE_NO = re.compile(r"^(no|nope|nah|not yet)[.!]?$", re.I)


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
            "Log baby sleep to Huckleberry. Three actions: 'start' when the baby is asleep "
            "and has not woken up yet ('down now', or 'sleep start 5:40am' for a sleep that "
            "began earlier and is still going), 'complete' when they have woken up and a "
            "sleep was already running ('awake', 'awake at 7:30'), or 'log' for a finished "
            "past sleep given both a start and end time ('slept 2pm to 4pm'). A sleep that is "
            "still in progress is always 'start', never 'log' with the end left blank."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "complete", "log"],
                    "description": "start = asleep now, still sleeping; complete = awake now; log = a finished past interval.",
                },
                "start_time": {
                    "type": "string",
                    "description": (
                        "24-hour 'HH:MM' the sleep began. Required for action=log. Optional for "
                        "action=start: give it only if the message says when the sleep began, "
                        "otherwise omit it and the timer starts now."
                    ),
                },
                "end_time": {
                    "type": "string",
                    "description": (
                        "24-hour 'HH:MM' the sleep ended. Required for action=log. Optional for "
                        "action=complete: give it only if the message says when they woke, "
                        "otherwise omit it and the timer stops now."
                    ),
                },
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
            return self._timed_args("complete", "end_time", text, tz)
        if _START.search(text):
            return self._timed_args("start", "start_time", text, tz)
        return None

    @staticmethod
    def _timed_args(action: str, field: str, text: str, tz: ZoneInfo) -> dict[str, Any]:
        """An action, backdated if the message named a clock time.

        `allow_now=False` keeps 'down now' and a bare 'awake' plain: resolving 'now' to a
        timestamp and writing it back as an explicit time would be the same instant with
        extra steps, and it would take the slower two-write path downstream.
        """
        when, _ = timeparse.resolve(text, tz, allow_now=False)
        return {"action": action, field: when} if when is not None else {"action": action}

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        action = raw.get("action")
        start_str = raw.get("start_time")
        end_str = raw.get("end_time")

        # Infer the action if the model gave times but no explicit action.
        if action not in ("start", "complete", "log"):
            action = "log" if (start_str and end_str) else MISSING

        # For start/complete the time is optional: absent (or 'now') means the timer runs
        # from, or stops at, this moment.
        if action == "start":
            when, explicit = resolve_llm_time(start_str, tz)
            return {"action": "start", "start_time": when} if explicit else {"action": "start"}

        if action == "complete":
            when, explicit = resolve_llm_time(end_str, tz)
            return {"action": "complete", "end_time": when} if explicit else {"action": "complete"}

        if action != "log":
            return {"action": action}

        start = self._time_or_missing(start_str, tz)
        end = self._time_or_missing(end_str, tz)
        # Overnight: an end at or before the start means it ended the next day.
        if isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
            end = end + timedelta(days=1)
        return {"action": "log", "start_time": start, "end_time": end}

    @staticmethod
    def _means_not_ended(text: str) -> bool:
        stripped = text.strip()
        return bool(_NOT_ENDED.search(stripped) or _BARE_NO.match(stripped))

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
                return "When did it end? e.g. `4pm`, or say `still asleep`"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        args = dict(args)
        action = args.get("action")

        if action == MISSING:
            if _WAKE.search(text):
                return self._timed_args("complete", "end_time", text, tz)
            if _START.search(text):
                return self._timed_args("start", "start_time", text, tz)
            return None

        if action == "log":
            # "she's still sleeping" answers the end-time question by saying there isn't one
            # yet. Turn the half-filled log into an open timer at the start we already have,
            # rather than returning None and letting the router replay the text as a new
            # message, which would re-parse it as a plain start and lose that start time.
            start_known = args.get("start_time") not in (None, MISSING)
            if start_known and args.get("end_time") == MISSING and self._means_not_ended(text):
                return {"action": "start", "start_time": coerce_dt(args["start_time"], tz)}

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
            started = args.get("start_time")
            return await self._start(coerce_dt(started, self._tz) if started else None)
        if action == "complete":
            ended = args.get("end_time")
            return await self._complete(coerce_dt(ended, self._tz) if ended else None)
        return await self._log(args)

    async def _start(self, started_at: datetime | None = None) -> SkillResult:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.start_sleep(child_uid)
            if started_at is not None:
                await self._backdate_timer(api, child_uid, started_at)
        except Exception as exc:
            log.exception("start_sleep failed")
            return SkillResult(ok=False, message=f"Couldn't start a sleep timer — {type(exc).__name__}.", error=f"{type(exc).__name__}: {exc}")
        began = started_at or common.now_minute(self._tz)
        return SkillResult(ok=True, message=f"Sleep started at {began:%H:%M}. Send `awake` when they're up.", verified=True)

    async def _backdate_timer(self, api: Any, child_uid: str, started_at: datetime) -> None:
        """Repoint the timer just opened by `start_sleep` at `started_at`.

        `start_sleep` stamps `time.time()` and takes no time argument, so opening a sleep
        that began earlier means writing the timestamp fields afterwards. Only three fields
        move; `start_sleep` has already written the rest of the document (session uuid,
        sleep conditions, locations) in the shape the app expects. Patching
        `timer.timerStartTime` is the one that counts: `complete_sleep` derives the logged
        interval from it, and the app renders elapsed time from it.
        """
        seconds = started_at.timestamp()
        db = await api._get_firestore_client()
        await db.collection("sleep").document(child_uid).update(
            {
                "timer.timerStartTime": seconds * 1000,  # the app stores this one in ms
                "timer.timestamp": {"seconds": seconds},
                "timer.local_timestamp": seconds,
            }
        )

    async def _complete(self, ended_at: datetime | None = None) -> SkillResult:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            if ended_at is not None:
                refusal = await self._preset_end(api, child_uid, ended_at)
                if refusal is not None:
                    return SkillResult(ok=True, verified=False, message=refusal)
            await api.complete_sleep(child_uid)
        except Exception as exc:
            log.warning("complete_sleep failed (likely no timer running): %s", exc)
            return SkillResult(
                ok=True, verified=False,
                message="Couldn't stop a sleep timer — is one running? Start one with `down now`, or log a past nap like `slept 2 to 4`.",
            )
        ended = ended_at or common.now_minute(self._tz)
        return SkillResult(ok=True, message=f"Awake — sleep logged, up at {ended:%H:%M}.", verified=True)

    async def _preset_end(self, api: Any, child_uid: str, ended_at: datetime) -> str | None:
        """Arm the running timer to end at `ended_at`. Returns a refusal message, or None.

        `complete_sleep` takes no end time, but it honours `timerEndTime` when the timer is
        paused — the app's own pause-then-stop path. Setting those two fields and completing
        immediately after reuses the library's whole completion routine (interval write,
        prefs.lastSleep, timer reset) instead of reimplementing it here.

        The read this needs is worth having anyway: `complete_sleep` returns quietly when no
        timer is active, so without it a backdated `awake` against a stopped timer would
        report a sleep that was never logged.
        """
        db = await api._get_firestore_client()
        doc_ref = db.collection("sleep").document(child_uid)
        snapshot = await doc_ref.get(timeout=10.0)
        timer = ((snapshot.to_dict() or {}).get("timer") or {}) if snapshot.exists else {}

        if not timer.get("active"):
            return (
                f"No sleep timer is running, so there's nothing to end at {ended_at:%H:%M}. "
                "Log the whole nap instead, like `slept 5:40 to 7:30`."
            )

        end_ms = ended_at.timestamp() * 1000
        start_ms = timer.get("timerStartTime")
        if start_ms and end_ms <= float(start_ms):
            started = datetime.fromtimestamp(float(start_ms) / 1000, self._tz)
            return (
                f"That sleep started at {started:%H:%M}, so it can't have ended at "
                f"{ended_at:%H:%M}. Send `awake` for now, or log it as a past nap."
            )

        await doc_ref.update(
            {
                "timer.paused": True,   # the flag complete_sleep checks before trusting the end
                "timer.active": True,
                "timer.timerEndTime": end_ms,
            }
        )
        return None

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
