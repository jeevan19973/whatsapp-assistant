"""Help skill — answers questions *about* the bot instead of logging them.

Registered first, so it gets the regex fast path before any tracker does. Without it a
message like "how do I log nursing" hits NursingSkill's keyword match and opens a real
nursing timer in Huckleberry: the user asked a question and got an entry they then have to
delete in the app.

The guard is deliberately narrow. A message has to look like a question *and* be about the
bot's own capabilities before this skill claims it, so "big green poo" and "wet at 2pm?"
still reach their trackers. Everything ambiguous is left to fall through — a missed help
reply costs a /help; a false positive costs a lost entry.

This module also owns the canonical example list, and the router's /help text is built from
it, so the two can't drift apart.
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

from app.skills.base import SkillResult

# topic -> (label, rows of examples, trailing note). One row per printed line; the order
# here is the order /help prints. tests/test_help.py asserts every example still parses,
# so the bot can't advertise a phrasing it would then fail to understand.
TOPICS: dict[str, tuple[str, tuple[tuple[str, ...], ...], str]] = {
    "bottle": ("Bottles", (("11am 90ml breast milk", "90ml formula"),), ""),
    "diaper": ("Nappies", (("poo nappy", "wet nappy at 2pm", "big green poo"),), ""),
    "sleep": (
        "Sleep",
        (
            ("down now", "awake", "slept 2pm to 4pm"),
            ("sleep start 5:40am", "awake at 7:30"),
        ),
        "(either end can be backdated)",
    ),
    "nursing": ("Nursing", (("feeding now left", "nursing done"),), ""),
    "pump": ("Pump", (("pumped 120ml", "expressed 3oz left"),), ""),
    "growth": ("Growth", (("weight 5.2kg", "55cm 5.1kg"),), ""),
}


def examples_for(topic: str) -> list[str]:
    """Every example phrase advertised for a topic, flattened."""
    _, rows, _ = TOPICS[topic]
    return [example for row in rows for example in row]

# Words that point a question at one tracker, so "how do I log nursing" answers about
# nursing rather than reciting everything.
_TOPIC_WORDS: dict[str, str] = {
    "bottle": "bottle", "bottles": "bottle", "formula": "bottle", "milk": "bottle",
    "nappy": "diaper", "nappies": "diaper", "diaper": "diaper", "diapers": "diaper",
    "poo": "diaper", "pee": "diaper",
    "sleep": "sleep", "sleeping": "sleep", "nap": "sleep", "naps": "sleep", "asleep": "sleep",
    "nursing": "nursing", "nurse": "nursing", "breastfeed": "nursing",
    "breastfeeding": "nursing", "latch": "nursing",
    "pump": "pump", "pumping": "pump", "pumped": "pump", "express": "pump",
    "expressed": "pump",
    "growth": "growth", "weight": "growth", "height": "growth", "length": "growth",
}

# "What are…", "How do I…", "Can you…". A trailing '?' counts as a lead on its own.
_QUESTION_LEAD = re.compile(
    r"^\s*(what'?s?|how|which|who|where|why|can|could|do|does|are|is|should|would|will|"
    r"tell me|explain|any)\b",
    re.I,
)
# The question has to be about the bot itself. Without this, "did she poo?" would be
# answered with a help card instead of reaching the diaper tracker.
_ABOUT_THE_BOT = re.compile(
    r"\b(tool|tools|command|commands|feature|features|capabilit(y|ies)|support(ed|s)?|"
    r"option|options|help|log|logging|track|tracking|record|use|using|work|works|"
    r"you do|you handle|you track|you log|able to)\b",
    re.I,
)
# A bare cry for help, with no question mark and no interrogative to lead it.
_BARE_HELP = re.compile(r"^\s*(help( me)?|what can you do|commands)[?!.]*\s*$", re.I)


def _looks_like_a_question(text: str) -> bool:
    return bool(_QUESTION_LEAD.search(text) or text.rstrip().endswith("?"))


def _topic_in(text: str) -> str | None:
    lowered = text.lower()
    for word, topic in _TOPIC_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return topic
    return None


def _block(label: str, rows: tuple[tuple[str, ...], ...], note: str, width: int) -> str:
    lines = []
    for i, row in enumerate(rows):
        head = f"{label}:".ljust(width) if i == 0 else " " * width
        lines.append(head + " · ".join(row))
    if note:
        lines[-1] += f"  {note}"
    return "\n".join(lines)


def _examples(topic: str | None) -> str:
    width = max(len(label) for label, _, _ in TOPICS.values()) + 4
    if topic and topic in TOPICS:
        label, rows, note = TOPICS[topic]
        return _block(label, rows, note, len(label) + 4)
    return "\n".join(_block(label, rows, note, width) for label, rows, note in TOPICS.values())


HELP = (
    "I log baby entries to Huckleberry. Just tell me what happened.\n\n"
    f"{_examples(None)}\n\n"
    "Or just describe it in your own words and I'll work it out.\n\n"
    "Commands:  /help  /last  /retry\n"
    "Note: entries can't be deleted from here, use the Huckleberry app for that."
)


class HelpSkill:
    name = "help"

    tool_schema = {
        "name": "help",
        "description": (
            "Answer a question about what this bot can do or how to phrase an entry, e.g. "
            "'what can you track?', 'how do I log nursing?'. Use this for questions ABOUT "
            "logging. Never use it for an actual entry the user wants logged."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": sorted(TOPICS),
                    "description": "Which tracker the question is about, if it names one.",
                }
            },
            "required": [],
        },
    }

    # ---- parsing / clarification --------------------------------------------

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        if _BARE_HELP.match(text):
            return {"topic": None}
        if _looks_like_a_question(text) and _ABOUT_THE_BOT.search(text):
            return {"topic": _topic_in(text)}
        return None

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        topic = raw.get("topic")
        return {"topic": topic if topic in TOPICS else None}

    def missing_question(self, args: dict[str, Any]) -> str | None:
        return None  # nothing here is ever required

    def complete_pending(
        self, args: dict[str, Any], text: str, tz: ZoneInfo
    ) -> dict[str, Any] | None:
        return None  # this skill never leaves a question outstanding

    # ---- execution ----------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        topic = args.get("topic")
        if topic is None:
            return SkillResult(ok=True, message=HELP, verified=True)
        return SkillResult(
            ok=True,
            message=(
                f"{_examples(topic)}\n\n"
                "Or just describe it in your own words and I'll work it out. "
                "Send /help for everything I track."
            ),
            verified=True,
        )
