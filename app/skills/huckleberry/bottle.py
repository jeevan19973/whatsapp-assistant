"""Bottle-feed skill."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.parsers import bottle as bottle_parser
from app.parsers.common import MISSING, UNITS_WORDS, coerce_dt, resolve_llm_time
from app.skills.base import SkillResult
from app.skills.huckleberry.client import HuckleberryClient

log = logging.getLogger(__name__)


# The seven values Huckleberry accepts (PLAN.md §9, firebase_types.BottleType).
BOTTLE_TYPES = [
    "Breast Milk", "Formula", "Tube Feeding", "Cow Milk", "Goat Milk", "Soy Milk", "Other",
]


class BottleSkill:
    name = "huckleberry_bottle"

    # Published to the LLM tier (app/llm). Every field is optional: the model fills only
    # what the message states, and the router asks about anything left out rather than the
    # model guessing (PLAN.md decision 4).
    tool_schema = {
        "name": "huckleberry_bottle",
        "description": (
            "Log a baby bottle feed to Huckleberry. Use this when the message describes "
            "feeding the baby milk or formula, e.g. 'gave her 90 of breast milk around 11'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "description": "The volume fed. Omit if the message doesn't say.",
                },
                "units": {
                    "type": "string",
                    "enum": ["ml", "oz"],
                    "description": "Volume unit. Omit if not stated — never guess ml vs oz.",
                },
                "bottle_type": {
                    "type": "string",
                    "enum": BOTTLE_TYPES,
                    "description": "What was in the bottle. Omit if the message doesn't say.",
                },
                "time": {
                    "type": "string",
                    "description": (
                        "When the feed happened, as a 24-hour clock time 'HH:MM' — convert "
                        "the wording yourself ('11 this morning' -> '11:00', 'half seven in "
                        "the evening' -> '19:30', '7' at night -> '19:00'). Use 'now' or omit "
                        "if the message states no time."
                    ),
                },
            },
            "required": [],
        },
    }

    def __init__(self, client: HuckleberryClient) -> None:
        self._client = client

    # ---- parsing / clarification (the Skill interface) ----------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        return bottle_parser.parse(text, tz)

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        amount = raw.get("amount")
        units = raw.get("units")
        bottle_type = raw.get("bottle_type")
        when, explicit = resolve_llm_time(raw.get("time"), tz)
        return {
            "start_time": when,
            "amount": float(amount) if isinstance(amount, (int, float)) else MISSING,
            "units": units if units in {"ml", "oz"} else MISSING,
            "bottle_type": bottle_type if bottle_type in BOTTLE_TYPES else MISSING,
            "time_was_explicit": explicit,
        }

    def missing_question(self, args: dict[str, Any]) -> str | None:
        if args.get("amount") == MISSING:
            return "How much? e.g. `90ml`"
        if args.get("units") == MISSING:
            return f"Is {args['amount']:g} ml or oz?"
        if args.get("bottle_type") == MISSING:
            return "What was in it? e.g. `breast milk`, `formula`, `cow milk`"
        return None

    def complete_pending(
        self, args: dict[str, Any], text: str, tz: ZoneInfo
    ) -> dict[str, Any] | None:
        args = dict(args)
        args["start_time"] = coerce_dt(args.get("start_time"), tz)
        lowered = text.lower()
        resolved = False

        if args.get("bottle_type") == MISSING:
            for pattern, canonical in bottle_parser.BOTTLE_TYPES:
                if re.search(pattern, lowered, re.I):
                    args["bottle_type"] = canonical
                    resolved = True
                    break

        if args.get("units") == MISSING:
            for word, unit in UNITS_WORDS.items():
                if word in lowered:
                    args["units"] = unit
                    resolved = True
                    break

        if args.get("amount") == MISSING:
            fresh = bottle_parser.parse(text, tz)
            if fresh and fresh["amount"] != MISSING:
                args["amount"] = fresh["amount"]
                if fresh["units"] != MISSING:
                    args["units"] = fresh["units"]
                resolved = True

        return args if resolved else None

    # ---- execution ----------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        start: datetime = args["start_time"]
        amount = float(args["amount"])
        units = args["units"]
        bottle_type = args["bottle_type"]

        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.log_bottle(
                child_uid,
                start_time=start,
                amount=amount,
                bottle_type=bottle_type,
                units=units,
            )
        except Exception as exc:
            log.exception("log_bottle failed")
            return SkillResult(
                ok=False,
                message=(
                    f"Couldn't reach Huckleberry — {type(exc).__name__}. "
                    f"Saved locally as {amount:g}{units} {bottle_type} at "
                    f"{start:%H:%M}; retry with /retry."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

        verified = await self._verify(start, amount)

        pretty = f"{amount:g}{units} {bottle_type} at {start:%H:%M}"
        if not start.date() == datetime.now(start.tzinfo).date():
            pretty = f"{amount:g}{units} {bottle_type} at {start:%a %d %b %H:%M}"

        if verified:
            return SkillResult(ok=True, message=f"Bottle logged — {pretty}.", verified=True)
        # The write raised nothing, so it very likely landed; say so honestly rather than
        # claiming confirmation we don't have.
        return SkillResult(
            ok=True,
            message=f"Bottle sent — {pretty}. (Couldn't confirm it back; check the app.)",
            verified=False,
        )

    async def _verify(self, start: datetime, amount: float) -> bool:
        """Confirm the entry landed by matching it on read-back.

        Not via prefs.lastBottle: log_bottle only updates that when the new entry is the most
        recent one, so backdated entries would look like failures.
        """
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            now = datetime.now(start.tzinfo)
            entries = await api.list_feed_intervals(
                child_uid,
                start_time=min(start, now) - timedelta(minutes=5),
                end_time=now + timedelta(minutes=5),
            )
        except Exception:
            log.warning("verification read failed", exc_info=True)
            return False

        target = start.timestamp()
        for entry in entries:
            if getattr(entry, "mode", None) != "bottle":
                continue
            try:
                if abs(float(entry.start) - target) < 60 and abs(float(entry.amount) - amount) < 0.01:
                    return True
            except (AttributeError, TypeError, ValueError):
                continue
        return False
