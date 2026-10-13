"""Help skill, plus the guard that stops the bot advertising a phrasing it can't read.

The reported bug was 'Send `done` when finished' followed by "I didn't understand that".
The specific fix is copy; the general fix is test_every_advertised_example_parses, which
fails the build if any suggested phrase stops reaching a skill.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.skills.base import Registry
from app.skills.help import HELP, TOPICS, HelpSkill, examples_for
from app.skills.huckleberry.bottle import BottleSkill
from app.skills.huckleberry.diaper import DiaperSkill
from app.skills.huckleberry.growth import GrowthSkill
from app.skills.huckleberry.nursing import NursingSkill
from app.skills.huckleberry.pump import PumpSkill
from app.skills.huckleberry.sleep import SleepSkill

TZ = ZoneInfo("America/New_York")

# A range like "slept 2pm to 4pm" is handed to the LLM on purpose (see SleepSkill.regex_parse
# and NursingSkill.regex_parse) so overnight logic isn't duplicated in a regex. Those are the
# only advertised phrases allowed to miss the fast path.
_LLM_BY_DESIGN = {"slept 2pm to 4pm"}


def _skills():
    return [
        HelpSkill(),
        DiaperSkill(None),
        SleepSkill(None, TZ),
        NursingSkill(None, TZ),
        PumpSkill(None),
        GrowthSkill(None),
        BottleSkill(None),
    ]


def _claimed_by(text: str) -> str | None:
    for skill in _skills():
        if skill.regex_parse(text, TZ) is not None:
            return skill.name
    return None


def _reply_strings(source: str) -> list[str]:
    """Every string literal in a module except docstrings, which quote code identifiers
    (`start_nursing`, `allow_now=False`) rather than phrases a user would ever send."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


# ---- the guard --------------------------------------------------------------

@pytest.mark.parametrize(
    "example",
    [e for topic in TOPICS for e in examples_for(topic) if e not in _LLM_BY_DESIGN],
)
def test_every_advertised_example_parses(example: str):
    assert _claimed_by(example) is not None, (
        f"/help advertises {example!r} but no skill claims it"
    )


def test_suggested_phrases_in_skill_replies_parse():
    """Any `backticked` phrase in a skill's own reply copy has to be readable.

    Skipped for the clarification prompts, whose answers go through complete_pending rather
    than the fast path, and for the LLM-by-design ranges.
    """
    clarification_answers = {  # answers to a pending question, not standalone messages
        "90ml", "breast milk", "formula", "cow milk", "weight 5.2kg", "55cm", "head 38cm",
        "120ml", "2pm", "2:20pm", "4pm", "still asleep",
    }
    ranges = {"nursed 2 to 2:20", "slept 2 to 4", "slept 5:40 to 7:30"}
    skipped = clarification_answers | ranges | _LLM_BY_DESIGN

    root = Path(__file__).resolve().parents[1] / "app" / "skills"
    phrases: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for text in _reply_strings(path.read_text()):
            phrases.update(re.findall(r"`([^`]+)`", text))

    unreadable = sorted(p for p in phrases - skipped if _claimed_by(p) is None)
    assert not unreadable, f"skills suggest phrases nothing can parse: {unreadable}"


def test_nursing_start_suggests_a_phrase_it_can_read():
    """Regression: the reply said `done`, which parsed to nothing."""
    assert _claimed_by("nursing done") == "huckleberry_nursing"
    assert _claimed_by("done") is None  # still true, hence the copy change


# ---- question guard ---------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "What are tools available and how to log nursing",
        "how do I log a nappy?",
        "can you log sleep?",
        "what can you track",
        "does this support pumping?",
        "help",
        "What can you do",
    ],
)
def test_questions_are_answered_not_logged(text: str):
    assert _claimed_by(text) == "help"


@pytest.mark.parametrize(
    "text",
    [
        "big green poo",
        "90ml formula",
        "down now",
        "feeding now left",
        "wet nappy at 2pm",
        "pumped 120ml",
        "weight 5.2kg",
        "awake",
        "nursing done",
    ],
)
def test_real_entries_are_not_swallowed_by_help(text: str):
    assert _claimed_by(text) != "help"


def test_question_about_the_baby_is_not_a_help_question():
    """'did she poo?' is question-shaped but isn't about the bot, so help declines it."""
    assert HelpSkill().regex_parse("did she poo?", TZ) is None


def test_topic_question_answers_only_that_topic():
    skill = HelpSkill()
    args = skill.regex_parse("how do I log nursing?", TZ)
    assert args == {"topic": "nursing"}


async def test_topic_reply_shows_that_trackers_examples():
    result = await HelpSkill().execute({"topic": "nursing"})
    assert "feeding now left" in result.message
    assert "pumped 120ml" not in result.message


async def test_untargeted_question_returns_the_full_help():
    result = await HelpSkill().execute({"topic": None})
    assert result.message == HELP
    assert "Nappies:" in result.message and "Growth:" in result.message


def test_help_skill_registers_before_the_trackers():
    registry = Registry()
    for skill in _skills():
        registry.register(skill)
    assert registry.ordered()[0].name == "help"
