"""Router integration tests — real Journal, fake channel, fake skill. No network."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.channels.base import InboundMessage
from app.journal import Journal
from app.parsers.common import MISSING
from app.router import Router
from app.skills.base import Registry, SkillResult
from app.skills.huckleberry.bottle import BottleSkill
from app.skills.huckleberry.diaper import DiaperSkill
from app.skills.huckleberry.sleep import SleepSkill

TZ = ZoneInfo("America/New_York")
ALICE = "447700900123"
BOB = "447700900456"


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def verify_signature(self, body: bytes, header: str | None) -> bool:
        return True

    def parse(self, payload: dict) -> list[InboundMessage]:
        return []

    async def send_text(self, to: str, text: str) -> None:
        self.sent.append((to, text))

    async def close(self) -> None:
        pass


class FakeBottleSkill(BottleSkill):
    """Real parsing/clarify from BottleSkill; execute is faked so no network is touched."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        self.calls.append(args)
        if self.fail:
            return SkillResult(ok=False, message="Couldn't reach Huckleberry.", error="boom")
        return SkillResult(
            ok=True,
            message=f"Bottle logged — {args['amount']:g}{args['units']} {args['bottle_type']}.",
            verified=True,
        )


def make(fail: bool = False):
    journal = Journal(":memory:")
    channel = FakeChannel()
    skill = FakeBottleSkill(fail=fail)
    registry = Registry()
    registry.register(skill)
    router = Router(registry=registry, journal=journal, channel=channel, tz=TZ)
    return router, channel, skill, journal


def msg(text: str, sender: str = ALICE, mid: str = "wamid.1", kind: str = "text") -> InboundMessage:
    return InboundMessage(id=mid, sender=sender, kind=kind, text=text)


# ---- happy path ------------------------------------------------------------

async def test_canonical_entry_is_logged_and_confirmed():
    router, channel, skill, _ = make()
    await router.handle(msg("11am 90ml breast milk"))

    assert len(skill.calls) == 1
    call = skill.calls[0]
    assert call["amount"] == 90
    assert call["units"] == "ml"
    assert call["bottle_type"] == "Breast Milk"
    assert call["start_time"].hour == 11
    assert "time_was_explicit" not in call  # internal flag must not reach the skill

    assert len(channel.sent) == 1
    assert "90ml Breast Milk" in channel.sent[0][1]


async def test_confirmation_goes_only_to_the_sender():
    router, channel, _, _ = make()
    await router.handle(msg("90ml formula", sender=BOB))
    assert [to for to, _ in channel.sent] == [BOB]


# ---- dedupe ----------------------------------------------------------------

async def test_duplicate_message_id_is_not_logged_twice():
    """Meta retries deliveries; a retry must never double-log a feed."""
    router, channel, skill, _ = make()
    await router.handle(msg("90ml formula", mid="wamid.dup"))
    await router.handle(msg("90ml formula", mid="wamid.dup"))
    assert len(skill.calls) == 1
    assert len(channel.sent) == 1


async def test_dedupe_survives_a_restart():
    """The spike used an in-memory set, which would re-log after every deploy."""
    journal = Journal(":memory:")
    channel = FakeChannel()
    skill = FakeBottleSkill()
    registry = Registry()
    registry.register(skill)

    router_a = Router(registry=registry, journal=journal, channel=channel, tz=TZ)
    await router_a.handle(msg("90ml formula", mid="wamid.restart"))

    # New Router object, same journal — simulating a process restart.
    router_b = Router(registry=registry, journal=journal, channel=channel, tz=TZ)
    await router_b.handle(msg("90ml formula", mid="wamid.restart"))

    assert len(skill.calls) == 1


# ---- clarification ---------------------------------------------------------

async def test_missing_type_asks_instead_of_guessing():
    router, channel, skill, _ = make()
    await router.handle(msg("90ml"))
    assert skill.calls == []
    assert "What was in it?" in channel.sent[0][1]


async def test_clarification_answer_completes_the_entry():
    router, channel, skill, _ = make()
    await router.handle(msg("90ml", mid="wamid.a"))
    await router.handle(msg("formula", mid="wamid.b"))

    assert len(skill.calls) == 1
    assert skill.calls[0]["bottle_type"] == "Formula"
    assert skill.calls[0]["amount"] == 90


async def test_clarification_preserves_the_original_time():
    router, _, skill, _ = make()
    await router.handle(msg("11am 90ml", mid="wamid.a"))
    await router.handle(msg("breast milk", mid="wamid.b"))
    assert skill.calls[0]["start_time"].hour == 11


async def test_missing_units_asks_and_completes():
    router, channel, skill, _ = make()
    await router.handle(msg("90 formula", mid="wamid.a"))
    assert "ml or oz" in channel.sent[0][1]
    await router.handle(msg("ml", mid="wamid.b"))
    assert skill.calls[0]["units"] == "ml"


async def test_pending_question_is_isolated_per_sender():
    """Bob's 'formula' must not answer the question the bot asked Alice."""
    router, _, skill, journal = make()
    await router.handle(msg("90ml", sender=ALICE, mid="wamid.a"))
    await router.handle(msg("formula", sender=BOB, mid="wamid.b"))

    # Bob had no pending question, so his message is not treated as an answer.
    assert skill.calls == []
    assert await journal.get_pending(ALICE) is not None


async def test_unrelated_reply_clears_a_stale_question():
    router, channel, skill, journal = make()
    await router.handle(msg("90ml", mid="wamid.a"))
    await router.handle(msg("hello there", mid="wamid.b"))
    assert skill.calls == []
    assert await journal.get_pending(ALICE) is None


# ---- commands and failure modes -------------------------------------------

async def test_help_command():
    router, channel, _, _ = make()
    await router.handle(msg("/help"))
    assert "90ml breast milk" in channel.sent[0][1]


async def test_help_mentions_that_undo_is_impossible():
    """No delete method exists upstream — the bot must not imply otherwise."""
    router, channel, _, _ = make()
    await router.handle(msg("/help"))
    assert "can't be deleted" in channel.sent[0][1].lower()


async def test_last_command_lists_entries():
    router, channel, _, _ = make()
    await router.handle(msg("11am 90ml formula", mid="wamid.a"))
    await router.handle(msg("/last", mid="wamid.b"))
    assert "11am 90ml formula" in channel.sent[-1][1]


async def test_unknown_text_asks_for_help():
    router, channel, skill, _ = make()
    await router.handle(msg("what's the weather"))
    assert skill.calls == []
    assert "didn't understand" in channel.sent[0][1]


async def test_non_text_message_is_rejected_politely():
    router, channel, skill, _ = make()
    await router.handle(msg("", kind="image"))
    assert skill.calls == []
    assert "only read text" in channel.sent[0][1]


async def test_skill_failure_is_recorded_and_reported_honestly():
    router, channel, _, journal = make(fail=True)
    await router.handle(msg("90ml formula"))

    failed = await journal.failed_entries()
    assert len(failed) == 1
    assert failed[0]["error"] == "boom"
    assert "Couldn't reach Huckleberry" in channel.sent[0][1]


async def test_journal_records_sender_for_attribution():
    """Needed to answer 'who logged this?' later; painful to backfill."""
    router, _, _, journal = make()
    await router.handle(msg("90ml formula", sender=BOB))
    rows = await journal.recent()
    assert rows[0]["sender"] == BOB


# ---- sleep: the clarify loop must not lose a known start time ---------------

class FakeSleepSkill(SleepSkill):
    """Real parsing/clarify from SleepSkill; execute is faked so no network is touched."""

    def __init__(self) -> None:
        super().__init__(client=None, tz=TZ)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        self.calls.append(args)
        return SkillResult(ok=True, message="Sleep started.", verified=True)


def make_sleep():
    journal = Journal(":memory:")
    channel = FakeChannel()
    skill = FakeSleepSkill()
    registry = Registry()
    registry.register(skill)
    router = Router(registry=registry, journal=journal, channel=channel, tz=TZ)
    return router, channel, skill, journal


async def test_backdated_start_never_reaches_the_clarify_loop():
    router, channel, skill, _ = make_sleep()
    await router.handle(msg("Sleep start 5:40 am"))

    assert len(skill.calls) == 1
    args = skill.calls[0]
    assert args["action"] == "start"
    assert (args["start_time"].hour, args["start_time"].minute) == (5, 40)
    assert "end" not in channel.sent[0][1].lower()


async def test_still_sleeping_reply_keeps_the_pending_start_time():
    """Regression: 'She is still sleeping' answering 'when did it end?' opened a fresh
    timer at the time of the reply and dropped the 5:40 the user had already given."""
    router, _, skill, journal = make_sleep()
    started = datetime.now(TZ).replace(hour=5, minute=40, second=0, microsecond=0)
    await journal.set_pending(
        ALICE,
        "huckleberry_sleep",
        {"action": "log", "start_time": started, "end_time": MISSING},
        "When did it end? e.g. `4pm`, or say `still asleep`",
    )

    await router.handle(msg("She is still sleeping"))

    assert len(skill.calls) == 1
    args = skill.calls[0]
    assert args["action"] == "start"
    assert (args["start_time"].hour, args["start_time"].minute) == (5, 40)
    assert await journal.get_pending(ALICE) is None


# ---- diaper ----------------------------------------------------------------

class FakeDiaperSkill(DiaperSkill):
    """Real parsing/clarify from DiaperSkill; execute is faked so no network is touched."""

    def __init__(self) -> None:
        super().__init__(client=None)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        self.calls.append(args)
        return SkillResult(ok=True, message="Nappy logged.", verified=True)


def make_diaper():
    journal = Journal(":memory:")
    channel = FakeChannel()
    skill = FakeDiaperSkill()
    registry = Registry()
    registry.register(skill)
    router = Router(registry=registry, journal=journal, channel=channel, tz=TZ)
    return router, channel, skill, journal


async def test_diaper_intensity_synonym_reaches_the_skill():
    router, _, skill, _ = make_diaper()
    await router.handle(msg("Diaper pee heavy at 6:30 pm"))

    args = skill.calls[0]
    assert args["mode"] == "pee"
    assert args["pee_amount"] == "big"
    assert (args["start_time"].hour, args["start_time"].minute) == (18, 30)


async def test_diaper_amount_survives_the_clarifying_question():
    """Regression: '5:10 am diaper medium' -> 'Pee' logged the pee and dropped the medium.

    Goes through the real journal, so the scratch keys have to survive the JSON round-trip.
    """
    router, channel, skill, journal = make_diaper()
    await router.handle(msg("5:10 am diaper medium"))

    assert skill.calls == []
    assert "pee, poo" in channel.sent[0][1]

    await router.handle(msg("Pee", mid="wamid.2"))

    assert len(skill.calls) == 1
    args = skill.calls[0]
    assert args["mode"] == "pee"
    assert args["pee_amount"] == "medium"
    assert (args["start_time"].hour, args["start_time"].minute) == (5, 10)
    assert await journal.get_pending(ALICE) is None


async def test_diaper_mixed_nappy_asks_which_amount_then_logs():
    router, channel, skill, journal = make_diaper()
    await router.handle(msg("big nappy, poo and wee"))

    assert skill.calls == []
    assert "pee or the poo" in channel.sent[0][1]

    await router.handle(msg("poo", mid="wamid.2"))

    args = skill.calls[0]
    assert args["mode"] == "both"
    assert args["poo_amount"] == "big"
    assert args["pee_amount"] is None
    assert "amount_hint" not in args and "amount_target" not in args
    assert "source_text" not in args
    assert await journal.get_pending(ALICE) is None
