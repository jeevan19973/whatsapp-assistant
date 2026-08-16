"""Pump skill — logs an expressed-milk pumping session."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.parsers import common, timeparse
from app.parsers.common import MISSING, UNITS_WORDS, coerce_dt, resolve_llm_time
from app.skills.base import SkillResult
from app.skills.huckleberry.client import HuckleberryClient

log = logging.getLogger(__name__)

_PUMP_WORD = re.compile(r"\b(pump(ed|ing)?|express(ed|ing)?)\b", re.I)
_BARE_NUMBER = re.compile(r"\b(\d+(?:\.\d+)?)\b")


class PumpSkill:
    name = "huckleberry_pump"

    tool_schema = {
        "name": "huckleberry_pump",
        "description": (
            "Log a breast-pump / expressing session to Huckleberry. Use when the message "
            "describes pumping or expressing milk, e.g. 'pumped 120ml', 'expressed 3oz'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "total_amount": {
                    "type": "number",
                    "description": "Total volume pumped. Omit if the message doesn't say.",
                },
                "left_amount": {"type": "number", "description": "Left side, if stated separately."},
                "right_amount": {"type": "number", "description": "Right side, if stated separately."},
                "units": {
                    "type": "string",
                    "enum": ["ml", "oz"],
                    "description": "Volume unit. Omit if not stated — never guess ml vs oz.",
                },
                "time": {
                    "type": "string",
                    "description": "24-hour clock 'HH:MM', or 'now'/omit if no time is stated.",
                },
            },
            "required": [],
        },
    }

    def __init__(self, client: HuckleberryClient) -> None:
        self._client = client

    # ---- parsing / clarification --------------------------------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        if not _PUMP_WORD.search(text):
            return None
        when, remainder = timeparse.resolve(text, tz)
        vol = common.parse_volume(remainder)
        start = when or common.now_minute(tz)
        amount, units = (vol[0], vol[1]) if vol else (MISSING, MISSING)
        return {
            "start_time": start,
            "amount": amount,
            "units": units,
            "left": None,
            "right": None,
            "time_was_explicit": when is not None,
        }

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        total = raw.get("total_amount")
        left = raw.get("left_amount")
        right = raw.get("right_amount")
        units = raw.get("units")
        when, explicit = resolve_llm_time(raw.get("time"), tz)

        # If only per-side amounts were given, the total is their sum.
        if not isinstance(total, (int, float)) and isinstance(left, (int, float)) and isinstance(right, (int, float)):
            total = left + right

        return {
            "start_time": when,
            "amount": float(total) if isinstance(total, (int, float)) else MISSING,
            "units": units if units in {"ml", "oz"} else MISSING,
            "left": float(left) if isinstance(left, (int, float)) else None,
            "right": float(right) if isinstance(right, (int, float)) else None,
            "time_was_explicit": explicit,
        }

    def missing_question(self, args: dict[str, Any]) -> str | None:
        if args.get("amount") == MISSING:
            return "How much did you pump? e.g. `120ml`"
        if args.get("units") == MISSING:
            return f"Is {args['amount']:g} ml or oz?"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        args = dict(args)
        args["start_time"] = coerce_dt(args.get("start_time"), tz)
        lowered = text.lower()
        resolved = False

        if args.get("units") == MISSING:
            for word, unit in UNITS_WORDS.items():
                if word in lowered:
                    args["units"] = unit
                    resolved = True
                    break

        if args.get("amount") == MISSING:
            vol = common.parse_volume(text)
            if vol:
                args["amount"], args["units"] = vol[0], vol[1]
                resolved = True
            elif m := _BARE_NUMBER.search(text):
                args["amount"] = float(m.group(1))
                resolved = True

        return args if resolved else None

    # ---- execution ----------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        start: datetime = args["start_time"]
        total = float(args["amount"])
        units = args["units"]
        left = args.get("left")
        right = args.get("right")

        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.log_pump(
                child_uid,
                start_time=start,
                total_amount=total,
                left_amount=left,
                right_amount=right,
                units=units,
            )
        except Exception as exc:
            log.exception("log_pump failed")
            return SkillResult(
                ok=False,
                message=(
                    f"Couldn't reach Huckleberry — {type(exc).__name__}. "
                    f"Saved locally as {total:g}{units} pumped at {start:%H:%M}; retry with /retry."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

        verified = await self._verify(start)
        pretty = f"{total:g}{units} pumped at {start:%H:%M}"
        if start.date() != datetime.now(start.tzinfo).date():
            pretty = f"{total:g}{units} pumped at {start:%a %d %b %H:%M}"

        if verified:
            return SkillResult(ok=True, message=f"Pump logged — {pretty}.", verified=True)
        return SkillResult(
            ok=True,
            message=f"Pump sent — {pretty}. (Couldn't confirm it back; check the app.)",
            verified=False,
        )

    async def _verify(self, start: datetime) -> bool:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            now = datetime.now(start.tzinfo)
            entries = await api.list_pump_intervals(
                child_uid,
                start_time=min(start, now) - timedelta(minutes=5),
                end_time=now + timedelta(minutes=5),
            )
        except Exception:
            log.warning("pump verification read failed", exc_info=True)
            return False
        return common.entry_landed(entries, start)
