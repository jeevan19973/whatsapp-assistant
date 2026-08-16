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

# Huckleberry only stores three amounts, but people describe a nappy with whatever word
# comes to hand at 5am. These tables are the single source of truth for both tiers: the
# regex fast path reads them off the message, and coerce_llm runs the LLM's answer through
# them before falling back to None, so a stray "heavy" isn't silently dropped.
_AMOUNT_WORDS = {
    "little": "little", "small": "little", "tiny": "little", "light": "little",
    "slight": "little", "minimal": "little", "damp": "little", "barely": "little",
    "bit": "little", "spot": "little", "spotting": "little", "smidge": "little",
    "medium": "medium", "med": "medium", "moderate": "medium", "normal": "medium",
    "average": "medium", "regular": "medium", "usual": "medium",
    "big": "big", "large": "big", "huge": "big", "lots": "big", "lot": "big",
    "loads": "big", "tons": "big", "plenty": "big", "heavy": "big", "full": "big",
    "soaked": "big", "soaking": "big", "sodden": "big", "drenched": "big",
    "loaded": "big", "massive": "big", "enormous": "big", "giant": "big",
    "blowout": "big",
}
_CONSISTENCY_WORDS = {
    "solid": "solid", "firm": "solid", "formed": "solid",
    "loose": "loose", "soft": "loose", "mushy": "loose",
    "runny": "runny", "watery": "runny", "liquid": "runny",
    "mucousy": "mucousy", "mucusy": "mucousy", "mucus": "mucousy",
    "slimy": "mucousy", "stringy": "mucousy",
    "hard": "hard", "constipated": "hard",
    "pebbles": "pebbles", "pebbly": "pebbles", "pellets": "pebbles",
    "diarrhea": "diarrhea", "diarrhoea": "diarrhea",
}
_COLOR_WORDS = {
    **{c: c for c in COLORS},
    "grey": "gray", "mustard": "yellow", "gold": "yellow", "tan": "brown",
}

# "light brown" is a colour, not a little nappy — strip the phrase before reading amounts.
_LIGHT_COLOR = re.compile(
    r"\b(?:light|dark)\s+(?:" + "|".join((*COLORS, "grey")) + r")\b", re.I
)


def _find_word(text: str, table: dict[str, str]) -> str | None:
    lowered = text.lower()
    for word, canonical in table.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return canonical
    return None


def _find_color(text: str) -> str | None:
    return _find_word(text, _COLOR_WORDS)


def _find_amount(text: str) -> str | None:
    return _find_word(_LIGHT_COLOR.sub(" ", text), _AMOUNT_WORDS)


def _normalize(value: Any, table: dict[str, str], allowed: tuple[str, ...]) -> str | None:
    """Coerce a free-text value onto an enum, via the synonym table if it isn't already a
    member. Shared by the LLM tier so 'heavy' lands on 'big' rather than on None."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in allowed:
        return lowered
    return table.get(lowered)


def _details(text: str) -> dict[str, Any]:
    """Every optional detail a message carries, read without reference to the mode — so a
    detail stated before we know whether it was pee or poo survives the clarification."""
    return {
        "amount_hint": _find_amount(text),
        "color": _find_color(text),
        "consistency": _find_word(text, _CONSISTENCY_WORDS),
        "diaper_rash": bool(re.search(r"\brash\b", text, re.I)),
    }


def _answer_mode(text: str) -> str | None:
    lowered = text.lower()
    if "both" in lowered or (_POO.search(text) and _PEE.search(text)):
        return "both"
    if _POO.search(text):
        return "poo"
    if _PEE.search(text):
        return "pee"
    if _DRY.search(text):
        return "dry"
    return None


def _bind(args: dict[str, Any]) -> dict[str, Any]:
    """Fold the mode-agnostic details onto the fields the mode allows.

    Called from every entry point once the mode is known. While the mode is MISSING the
    details just sit on the args untouched, which is what keeps 'diaper medium' -> 'pee'
    from losing the medium.
    """
    mode = args.get("mode")
    hint = args.get("amount_hint")
    args.pop("amount_target", None)

    if mode == MISSING:
        return args

    if mode == "pee":
        args["pee_amount"] = args.get("pee_amount") or hint
        args["poo_amount"] = None
        args["color"] = None
        args["consistency"] = None
    elif mode == "poo":
        args["poo_amount"] = args.get("poo_amount") or hint
        args["pee_amount"] = None
    elif mode == "both":
        if hint and not args.get("pee_amount") and not args.get("poo_amount"):
            # Huckleberry keeps pee and poo amounts as separate fields, so one amount on a
            # mixed nappy is genuinely ambiguous. Ask rather than guess.
            args["amount_target"] = MISSING
    elif mode == "dry":
        args["pee_amount"] = None
        args["poo_amount"] = None
        args["color"] = None
        args["consistency"] = None
    return args


_AMOUNT_HINT = (
    "How much, on Huckleberry's three-point scale. Map the parent's own word onto it: "
    "light, small, damp or barely wet -> little; normal or average -> medium; "
    "heavy, soaked, full, loaded or blowout -> big. Omit only if no amount was given."
)


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
                "poo_amount": {
                    "type": "string",
                    "enum": list(AMOUNTS),
                    "description": _AMOUNT_HINT,
                },
                "pee_amount": {
                    "type": "string",
                    "enum": list(AMOUNTS),
                    "description": _AMOUNT_HINT,
                },
                "color": {
                    "type": "string",
                    "enum": list(COLORS),
                    "description": "Poo colour, if stated. Map grey to gray, mustard or gold to yellow.",
                },
                "consistency": {
                    "type": "string",
                    "enum": list(CONSISTENCIES),
                    "description": (
                        "Poo consistency, if stated. Map firm or formed to solid, soft or "
                        "mushy to loose, watery or liquid to runny, pellets to pebbles."
                    ),
                },
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
        args = {
            "start_time": start,
            "mode": mode,
            "poo_amount": None,
            "pee_amount": None,
            "time_was_explicit": when is not None,
            "source_text": text,
            **_details(text),
        }
        return _bind(args)

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        when, explicit = resolve_llm_time(raw.get("time"), tz)
        mode = raw.get("mode")
        mode = mode.strip().lower() if isinstance(mode, str) else mode
        poo_amount = _normalize(raw.get("poo_amount"), _AMOUNT_WORDS, AMOUNTS)
        pee_amount = _normalize(raw.get("pee_amount"), _AMOUNT_WORDS, AMOUNTS)

        args = {
            "start_time": when,
            "mode": mode if mode in MODES else MISSING,
            "poo_amount": poo_amount,
            "pee_amount": pee_amount,
            "amount_hint": poo_amount or pee_amount,
            "color": _normalize(raw.get("color"), _COLOR_WORDS, COLORS),
            "consistency": _normalize(raw.get("consistency"), _CONSISTENCY_WORDS, CONSISTENCIES),
            "diaper_rash": bool(raw.get("diaper_rash")),
            "time_was_explicit": explicit,
            "source_text": None,
        }
        return _bind(args)

    def missing_question(self, args: dict[str, Any]) -> str | None:
        if args.get("mode") == MISSING:
            return "Was it pee, poo, both, or dry?"
        if args.get("amount_target") == MISSING:
            hint = args.get("amount_hint") or "that"
            return f"Mixed nappy. Is the {hint} amount for the pee or the poo? (or both)"
        return None

    def complete_pending(self, args: dict[str, Any], text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        args = dict(args)
        args["start_time"] = coerce_dt(args.get("start_time"), tz)

        if args.get("mode") == MISSING:
            mode = _answer_mode(text)
            if mode is None:
                return None
            args["mode"] = mode
            # The answer may carry detail of its own ("pee, big"), and the original message
            # is re-read as a backstop so nothing parsed before the mode was known is lost.
            answer = _details(text)
            source = _details(args.get("source_text") or "")
            for key in ("amount_hint", "color", "consistency"):
                args[key] = answer[key] or source[key] or args.get(key)
            args["diaper_rash"] = bool(
                answer["diaper_rash"] or source["diaper_rash"] or args.get("diaper_rash")
            )
            return _bind(args)

        if args.get("amount_target") == MISSING:
            hint = args.get("amount_hint")
            target = _answer_mode(text)
            if target == "pee":
                args["pee_amount"] = hint
            elif target == "poo":
                args["poo_amount"] = hint
            elif target == "both":
                args["pee_amount"] = args["poo_amount"] = hint
            else:
                return None
            args.pop("amount_target", None)
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
        poo_amount, pee_amount = args.get("poo_amount"), args.get("pee_amount")
        bits = [args.get("color"), args.get("consistency")]
        if args.get("mode") == "both":
            # Label them on a mixed nappy, where a bare "(big)" wouldn't say which.
            bits += [f"{poo_amount} poo" if poo_amount else None,
                     f"{pee_amount} pee" if pee_amount else None]
        else:
            bits.append(poo_amount or pee_amount)
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
