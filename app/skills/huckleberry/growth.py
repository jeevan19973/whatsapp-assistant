"""Growth skill — logs a weight / height / head-circumference measurement."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.parsers import common, timeparse
from app.parsers.common import resolve_llm_time
from app.skills.base import SkillResult
from app.skills.huckleberry.client import HuckleberryClient

log = logging.getLogger(__name__)

_KG = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|kilo\w*)", re.I)
_G = re.compile(r"(\d+(?:\.\d+)?)\s*(g|gram\w*)\b", re.I)
_LB = re.compile(r"(\d+(?:\.\d+)?)\s*(lb|lbs|pound\w*)", re.I)
_CM = re.compile(r"(\d+(?:\.\d+)?)\s*(cm|centimet\w*)", re.I)
_IN = re.compile(r"(\d+(?:\.\d+)?)\s*(in|inch\w*)\b", re.I)
_HEAD = re.compile(r"\bhead\b", re.I)
_GROWTH_WORD = re.compile(r"\b(weigh\w*|weight|height|length|tall|grew|growth|head)\b", re.I)


class GrowthSkill:
    name = "huckleberry_growth"

    tool_schema = {
        "name": "huckleberry_growth",
        "description": (
            "Log a growth measurement (weight, height/length, or head circumference) to "
            "Huckleberry, e.g. 'weight 5.2kg', '55cm 5.1kg', 'head 38cm'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "weight": {"type": "number", "description": "Weight, in the unit implied by 'units'."},
                "height": {"type": "number", "description": "Height/length."},
                "head": {"type": "number", "description": "Head circumference."},
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "metric = kg/cm, imperial = lb/in. Default metric.",
                },
                "time": {"type": "string", "description": "24-hour 'HH:MM', or 'now'/omit."},
            },
            "required": [],
        },
    }

    def __init__(self, client: HuckleberryClient) -> None:
        self._client = client

    # ---- parsing / clarification --------------------------------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        if not (_GROWTH_WORD.search(text) or _KG.search(text) or _LB.search(text)):
            return None

        weight = height = head = None
        units = "metric"

        if m := _KG.search(text):
            weight, units = float(m.group(1)), "metric"
        elif m := _LB.search(text):
            weight, units = float(m.group(1)), "imperial"
        elif m := _G.search(text):
            weight, units = float(m.group(1)) / 1000, "metric"

        length = _CM.search(text) or _IN.search(text)
        if length:
            val = float(length.group(1))
            is_metric = bool(_CM.search(text))
            if _HEAD.search(text):
                head = val
            else:
                height = val
            if weight is None:
                units = "metric" if is_metric else "imperial"

        if weight is None and height is None and head is None:
            return None

        when, _ = timeparse.resolve(text, tz)
        return {
            "start_time": when or common.now_minute(tz),
            "weight": weight,
            "height": height,
            "head": head,
            "units": units,
            "time_was_explicit": when is not None,
        }

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        when, explicit = resolve_llm_time(raw.get("time"), tz)

        def num(v: Any) -> float | None:
            return float(v) if isinstance(v, (int, float)) else None

        units = raw.get("units") if raw.get("units") in ("metric", "imperial") else "metric"
        return {
            "start_time": when,
            "weight": num(raw.get("weight")),
            "height": num(raw.get("height")),
            "head": num(raw.get("head")),
            "units": units,
            "time_was_explicit": explicit,
        }

    def missing_question(self, args: dict[str, Any]) -> str | None:
        if args.get("weight") is None and args.get("height") is None and args.get("head") is None:
            return "What did you measure? e.g. `weight 5.2kg`, `55cm`, or `head 38cm`"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        # A follow-up measurement answer is just a fresh growth message.
        return self.regex_parse(text, tz)

    # ---- execution ----------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        start: datetime = args["start_time"]
        weight = args.get("weight")
        height = args.get("height")
        head = args.get("head")
        units = args.get("units", "metric")

        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.log_growth(
                child_uid, start_time=start, weight=weight, height=height, head=head, units=units
            )
        except Exception as exc:
            log.exception("log_growth failed")
            return SkillResult(
                ok=False,
                message=(
                    f"Couldn't reach Huckleberry — {type(exc).__name__}. "
                    f"Saved locally ({self._detail(weight, height, head, units)}); retry with /retry."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

        return SkillResult(
            ok=True,
            message=f"Growth logged — {self._detail(weight, height, head, units)}.",
            verified=False,
        )

    @staticmethod
    def _detail(weight: float | None, height: float | None, head: float | None, units: str) -> str:
        wu, lu = ("kg", "cm") if units == "metric" else ("lb", "in")
        bits = []
        if weight is not None:
            bits.append(f"{weight:g}{wu}")
        if height is not None:
            bits.append(f"{height:g}{lu}")
        if head is not None:
            bits.append(f"head {head:g}{lu}")
        return ", ".join(bits) if bits else "no measurement"
