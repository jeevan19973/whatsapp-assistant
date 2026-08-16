"""Diaper skill — logs a nappy change with optional colour / consistency / amount."""

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

# firebase_types enums (PLAN.md §9).
MODES = ("pee", "poo", "both", "dry")
COLORS = ("yellow", "brown", "black", "green", "red", "gray")
CONSISTENCIES = ("solid", "loose", "runny", "mucousy", "hard", "pebbles", "diarrhea")
AMOUNTS = ("little", "medium", "big")

_NAPPY_WORD = re.compile(r"\b(nappy|nappies|diaper|diapers|changed?)\b", re.I)
_POO = re.compile(r"\b(poo|poop|poos|pooped|dirty|soiled|bm)\b", re.I)
_PEE = re.compile(r"\b(pee|peed|wee|weed|wet)\b", re.I)
_DRY = re.compile(r"\bdry\b", re.I)
# "wet"/"dry" need nappy context; poo/pee words are diaper context on their own.
_PEE_STANDALONE = re.compile(r"\b(pee|peed|wee|weed)\b", re.I)

_AMOUNT_WORDS = {
    "little": "little", "small": "little", "tiny": "little",
    "medium": "medium", "med": "medium", "moderate": "medium",
    "big": "big", "large": "big", "huge": "big", "lots": "big",
}
_CONSISTENCY_WORDS = {
    "solid": "solid", "loose": "loose", "runny": "runny", "watery": "runny",
    "mucousy": "mucousy", "mucusy": "mucousy", "mucus": "mucousy",
    "hard": "hard", "pebbles": "pebbles", "pebbly": "pebbles",
    "diarrhea": "diarrhea", "diarrhoea": "diarrhea",
}


def _find_color(text: str) -> str | None:
    lowered = text.lower()
    if "grey" in lowered or "gray" in lowered:
        return "gray"
    for c in COLORS:
        if re.search(rf"\b{c}\b", lowered):
            return c
    return None


def _find_word(text: str, table: dict[str, str]) -> str | None:
    lowered = text.lower()
    for word, canonical in table.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return canonical
    return None


class DiaperSkill:
    name = "huckleberry_diaper"

    tool_schema = {
        "name": "huckleberry_diaper",
        "description": (
            "Log a nappy/diaper change to Huckleberry. Use for messages about wet or dirty "
            "nappies, e.g. 'poo nappy', 'wet at 2pm', 'big green poo'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": "pee (wet), poo (dirty), both, or dry. Omit if unclear.",
                },
                "poo_amount": {"type": "string", "enum": list(AMOUNTS)},
                "pee_amount": {"type": "string", "enum": list(AMOUNTS)},
                "color": {"type": "string", "enum": list(COLORS), "description": "Poo colour, if stated."},
                "consistency": {"type": "string", "enum": list(CONSISTENCIES), "description": "Poo consistency, if stated."},
                "diaper_rash": {"type": "boolean", "description": "True only if a rash is mentioned."},
                "time": {"type": "string", "description": "24-hour 'HH:MM', or 'now'/omit."},
            },
            "required": [],
        },
    }

    def __init__(self, client: HuckleberryClient) -> None:
        self._client = client

    # ---- parsing / clarification --------------------------------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        has_nappy = bool(_NAPPY_WORD.search(text))
        poo = bool(_POO.search(text))
        pee_standalone = bool(_PEE_STANDALONE.search(text))
        wet = bool(re.search(r"\bwet\b", text, re.I))
        dry = bool(_DRY.search(text))

        # Trigger only with clear diaper context, so "wet wipes" doesn't false-match.
        pee = pee_standalone or (wet and has_nappy)
        dry_ok = dry and has_nappy
        if not (poo or pee or dry_ok or has_nappy):
            return None

        if poo and pee:
            mode: Any = "both"
        elif poo:
            mode = "poo"
        elif pee:
            mode = "pee"
        elif dry_ok:
            mode = "dry"
        else:
            mode = MISSING  # bare "changed her nappy" — ask what it was

        when, _ = timeparse.resolve(text, tz)
        start = when or common.now_minute(tz)
        amount = _find_word(text, _AMOUNT_WORDS)
        args = {
            "start_time": start,
            "mode": mode,
            "poo_amount": amount if mode in ("poo", "both") else None,
            "pee_amount": amount if mode == "pee" else None,
            "color": _find_color(text) if mode in ("poo", "both") else None,
            "consistency": _find_word(text, _CONSISTENCY_WORDS) if mode in ("poo", "both") else None,
            "diaper_rash": bool(re.search(r"\brash\b", text, re.I)),
            "time_was_explicit": when is not None,
        }
        return args

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        when, explicit = resolve_llm_time(raw.get("time"), tz)
        mode = raw.get("mode")

        def pick(value: Any, allowed: tuple[str, ...]) -> Any:
            return value if value in allowed else None

        return {
            "start_time": when,
            "mode": mode if mode in MODES else MISSING,
            "poo_amount": pick(raw.get("poo_amount"), AMOUNTS),
            "pee_amount": pick(raw.get("pee_amount"), AMOUNTS),
            "color": pick(raw.get("color"), COLORS),
            "consistency": pick(raw.get("consistency"), CONSISTENCIES),
            "diaper_rash": bool(raw.get("diaper_rash")),
            "time_was_explicit": explicit,
        }

    def missing_question(self, args: dict[str, Any]) -> str | None:
        if args.get("mode") == MISSING:
            return "Was it pee, poo, both, or dry?"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        args = dict(args)
        args["start_time"] = coerce_dt(args.get("start_time"), tz)
        if args.get("mode") == MISSING:
            lowered = text.lower()
            if "both" in lowered:
                args["mode"] = "both"
            elif _POO.search(text):
                args["mode"] = "poo"
            elif _PEE.search(text):
                args["mode"] = "pee"
            elif _DRY.search(text):
                args["mode"] = "dry"
            else:
                return None
            return args
        return None

    # ---- execution ----------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        start: datetime = args["start_time"]
        mode = args["mode"]

        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            await api.log_diaper(
                child_uid,
                start_time=start,
                mode=mode,
                pee_amount=args.get("pee_amount"),
                poo_amount=args.get("poo_amount"),
                color=args.get("color"),
                consistency=args.get("consistency"),
                diaper_rash=bool(args.get("diaper_rash")),
            )
        except Exception as exc:
            log.exception("log_diaper failed")
            return SkillResult(
                ok=False,
                message=(
                    f"Couldn't reach Huckleberry — {type(exc).__name__}. "
                    f"Saved locally as {mode} nappy at {start:%H:%M}; retry with /retry."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

        verified = await self._verify(start)
        pretty = f"{mode}{self._detail(args)} at {start:%H:%M}"
        if start.date() != datetime.now(start.tzinfo).date():
            pretty = f"{mode}{self._detail(args)} at {start:%a %d %b %H:%M}"

        if verified:
            return SkillResult(ok=True, message=f"Nappy logged — {pretty}.", verified=True)
        return SkillResult(
            ok=True,
            message=f"Nappy sent — {pretty}. (Couldn't confirm it back; check the app.)",
            verified=False,
        )

    @staticmethod
    def _detail(args: dict[str, Any]) -> str:
        bits = [
            args.get("color"),
            args.get("consistency"),
            args.get("poo_amount") or args.get("pee_amount"),
        ]
        bits = [b for b in bits if b]
        if args.get("diaper_rash"):
            bits.append("rash")
        return f" ({', '.join(bits)})" if bits else ""

    async def _verify(self, start: datetime) -> bool:
        try:
            api = await self._client.api()
            child_uid = await self._client.child_uid()
            now = datetime.now(start.tzinfo)
            entries = await api.list_diaper_intervals(
                child_uid,
                start_time=min(start, now) - timedelta(minutes=5),
                end_time=now + timedelta(minutes=5),
            )
        except Exception:
            log.warning("diaper verification read failed", exc_info=True)
            return False
        return common.entry_landed(entries, start)
