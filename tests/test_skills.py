"""Unit tests for the new skills' parsing/coercion — no client, no network.

regex_parse / coerce_llm / missing_question don't touch the Huckleberry client, so the
skills are constructed with a None client here. Execution is covered live by spike_llm.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.parsers.common import MISSING
from app.skills.base import Registry
from app.skills.huckleberry.bottle import BottleSkill
from app.skills.huckleberry.diaper import DiaperSkill
from app.skills.huckleberry.growth import GrowthSkill
from app.skills.huckleberry.nursing import NursingSkill
from app.skills.huckleberry.pump import PumpSkill
from app.skills.huckleberry.sleep import SleepSkill

TZ = ZoneInfo("America/New_York")


def _skills():
    # Same order as main.py — keyword-specific first, bottle (volume catch-all) last.
    return [
        DiaperSkill(None),
        SleepSkill(None, TZ),
        NursingSkill(None, TZ),
        PumpSkill(None),
        GrowthSkill(None),
        BottleSkill(None),
    ]


# ---- pump -------------------------------------------------------------------

def test_pump_regex_extracts_volume():
    args = PumpSkill(None).regex_parse("pumped 120ml", TZ)
    assert args["amount"] == 120 and args["units"] == "ml"


def test_pump_without_volume_asks():
    skill = PumpSkill(None)
    args = skill.regex_parse("just pumped", TZ)
    assert args["amount"] == MISSING
    assert "How much" in skill.missing_question(args)


def test_pump_llm_sums_sides():
    args = PumpSkill(None).coerce_llm({"left_amount": 60, "right_amount": 50, "units": "ml"}, TZ)
    assert args["amount"] == 110 and args["left"] == 60 and args["right"] == 50


# ---- diaper -----------------------------------------------------------------

def test_diaper_poo_with_detail():
    args = DiaperSkill(None).regex_parse("big green poo", TZ)
    assert args["mode"] == "poo"
    assert args["color"] == "green"
    assert args["poo_amount"] == "big"


def test_diaper_wet_needs_nappy_context():
    skill = DiaperSkill(None)
    assert skill.regex_parse("wet nappy", TZ)["mode"] == "pee"
    assert skill.regex_parse("wet wipes", TZ) is None  # no false trigger


def test_diaper_bare_change_asks_mode():
    skill = DiaperSkill(None)
    args = skill.regex_parse("changed her nappy", TZ)
    assert args["mode"] == MISSING
    assert "pee, poo" in skill.missing_question(args)


def test_diaper_both_when_pee_and_poo():
    assert DiaperSkill(None).regex_parse("poo and wee in the nappy", TZ)["mode"] == "both"


def test_diaper_amount_synonyms():
    skill = DiaperSkill(None)
    assert skill.regex_parse("diaper pee heavy at 6:30pm", TZ)["pee_amount"] == "big"
    assert skill.regex_parse("light wet nappy", TZ)["pee_amount"] == "little"
    assert skill.regex_parse("soaked nappy, wet", TZ)["pee_amount"] == "big"
    assert skill.regex_parse("normal poo", TZ)["poo_amount"] == "medium"


def test_diaper_light_colour_is_not_an_amount():
    args = DiaperSkill(None).regex_parse("light brown poo", TZ)
    assert args["color"] == "brown"
    assert args["poo_amount"] is None


def test_diaper_amount_survives_the_mode_question():
    """'5:10 am diaper medium' -> 'pee' must keep the medium (issue 2B)."""
    skill = DiaperSkill(None)
    args = skill.regex_parse("5:10 am diaper medium", TZ)
    assert args["mode"] == MISSING
    assert args["amount_hint"] == "medium"

    done = skill.complete_pending(args, "Pee", TZ)
    assert done["mode"] == "pee"
    assert done["pee_amount"] == "medium"
    assert skill.missing_question(done) is None


def test_diaper_answer_can_carry_its_own_detail():
    """'pee, big' as the answer supplies the amount the first message lacked (issue 2C)."""
    skill = DiaperSkill(None)
    args = skill.regex_parse("changed her nappy", TZ)
    done = skill.complete_pending(args, "poo, big and green", TZ)
    assert done["mode"] == "poo"
    assert done["poo_amount"] == "big"
    assert done["color"] == "green"


def test_diaper_answer_detail_beats_the_original():
    skill = DiaperSkill(None)
    args = skill.regex_parse("nappy change, medium", TZ)
    done = skill.complete_pending(args, "pee, actually heavy", TZ)
    assert done["pee_amount"] == "big"


def test_diaper_mixed_with_one_amount_asks_which():
    skill = DiaperSkill(None)
    args = skill.regex_parse("big nappy, poo and wee", TZ)
    assert args["mode"] == "both"
    question = skill.missing_question(args)
    assert question is not None and "pee or the poo" in question

    done = skill.complete_pending(args, "poo", TZ)
    assert done["poo_amount"] == "big"
    assert done["pee_amount"] is None
    assert skill.missing_question(done) is None


def test_diaper_mixed_amount_can_apply_to_both():
    skill = DiaperSkill(None)
    args = skill.regex_parse("medium poo and wee nappy", TZ)
    done = skill.complete_pending(args, "both", TZ)
    assert done["poo_amount"] == done["pee_amount"] == "medium"


def test_diaper_mixed_without_amount_asks_nothing():
    skill = DiaperSkill(None)
    args = DiaperSkill(None).regex_parse("poo and wee nappy", TZ)
    assert skill.missing_question(args) is None


def test_diaper_mode_question_then_amount_question():
    """A bare change answered with 'both' chains into the pee-or-poo question."""
    skill = DiaperSkill(None)
    args = skill.regex_parse("changed her nappy", TZ)
    after_mode = skill.complete_pending(args, "both, big", TZ)
    assert after_mode["mode"] == "both"
    assert "pee or the poo" in skill.missing_question(after_mode)

    done = skill.complete_pending(after_mode, "pee", TZ)
    assert done["pee_amount"] == "big"
    assert done["poo_amount"] is None


def test_diaper_llm_normalizes_off_enum_values():
    """The LLM answering 'heavy' must land on 'big', not be discarded (issue 1C)."""
    args = DiaperSkill(None).coerce_llm(
        {"mode": "Poo", "poo_amount": "heavy", "color": "grey", "consistency": "watery"},
        TZ,
    )
    assert args["mode"] == "poo"
    assert args["poo_amount"] == "big"
    assert args["color"] == "gray"
    assert args["consistency"] == "runny"


def test_diaper_llm_junk_still_drops():
    args = DiaperSkill(None).coerce_llm({"mode": "poo", "poo_amount": "sparkly"}, TZ)
    assert args["poo_amount"] is None


# ---- sleep ------------------------------------------------------------------

def test_sleep_timer_start_and_complete():
    skill = SleepSkill(None, TZ)
    assert skill.regex_parse("down now", TZ) == {"action": "start"}
    assert skill.regex_parse("she's awake", TZ) == {"action": "complete"}


def test_sleep_range_defers_to_llm():
    # A two-time range is a completed interval — regex returns None so the LLM handles it.
    assert SleepSkill(None, TZ).regex_parse("slept 2pm to 4pm", TZ) is None


def test_sleep_llm_log_handles_overnight():
    args = SleepSkill(None, TZ).coerce_llm(
        {"action": "log", "start_time": "19:00", "end_time": "06:00"}, TZ
    )
    assert args["end_time"] > args["start_time"]  # 06:00 rolled to next day


def test_sleep_backdated_start_from_regex():
    args = SleepSkill(None, TZ).regex_parse("Sleep start 5:40 am", TZ)
    assert args["action"] == "start"
    assert (args["start_time"].hour, args["start_time"].minute) == (5, 40)


def test_sleep_start_now_carries_no_explicit_time():
    # 'now' is the default, so it stays out of the args and the timer starts server-side.
    assert SleepSkill(None, TZ).regex_parse("down now", TZ) == {"action": "start"}
    assert SleepSkill(None, TZ).coerce_llm({"action": "start", "start_time": "now"}, TZ) == {"action": "start"}
    assert SleepSkill(None, TZ).coerce_llm({"action": "start"}, TZ) == {"action": "start"}


def test_sleep_llm_start_keeps_an_explicit_time():
    args = SleepSkill(None, TZ).coerce_llm({"action": "start", "start_time": "05:40"}, TZ)
    assert (args["start_time"].hour, args["start_time"].minute) == (5, 40)


def test_sleep_still_sleeping_keeps_the_known_start():
    # Regression: answering "when did it end?" with "she is still sleeping" used to drop the
    # start time and open a fresh timer at the time of the reply.
    skill = SleepSkill(None, TZ)
    pending = skill.coerce_llm({"action": "log", "start_time": "05:40"}, TZ)
    assert skill.missing_question(pending) is not None  # asks for the end time

    updated = skill.complete_pending(pending, "She is still sleeping", TZ)
    assert updated["action"] == "start"
    assert (updated["start_time"].hour, updated["start_time"].minute) == (5, 40)
    assert skill.missing_question(updated) is None  # ready to run, nothing more to ask


def test_sleep_bare_no_means_still_sleeping():
    skill = SleepSkill(None, TZ)
    pending = skill.coerce_llm({"action": "log", "start_time": "05:40"}, TZ)
    assert skill.complete_pending(pending, "not yet", TZ)["action"] == "start"


def test_sleep_no_inside_a_sentence_is_still_read_as_a_time():
    skill = SleepSkill(None, TZ)
    pending = skill.coerce_llm({"action": "log", "start_time": "05:40"}, TZ)
    updated = skill.complete_pending(pending, "no idea, about 7am", TZ)
    assert updated["action"] == "log" and updated["end_time"].hour == 7


# `start_sleep` takes no time argument, so a backdated start is a second write straight to
# the timer fields. These stubs pin the field names: `complete_sleep` reads timerStartTime
# back to compute the interval, so a typo here would silently log the wrong duration.

class _StubSnapshot:
    def __init__(self, data: dict | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict | None:
        return self._data


class _StubDoc:
    def __init__(self, data: dict | None = None) -> None:
        self.updates: list[dict] = []
        self._data = data

    async def get(self, timeout: float | None = None) -> _StubSnapshot:
        return _StubSnapshot(self._data)

    async def update(self, data: dict) -> None:
        self.updates.append(data)


class _StubApi:
    def __init__(self, doc_data: dict | None = None) -> None:
        self.doc = _StubDoc(doc_data)
        self.started: list[str] = []
        self.completed: list[str] = []

    async def start_sleep(self, child_uid: str) -> None:
        self.started.append(child_uid)

    async def complete_sleep(self, child_uid: str) -> None:
        self.completed.append(child_uid)

    async def _get_firestore_client(self):
        class _Db:
            def __init__(self, doc):
                self._doc = doc

            def collection(self, name):
                assert name == "sleep"
                return self

            def document(self, uid):
                return self._doc

        return _Db(self.doc)


class _StubClient:
    def __init__(self, api: _StubApi) -> None:
        self._api = api

    async def api(self) -> _StubApi:
        return self._api

    async def child_uid(self) -> str:
        return "child-1"


async def test_backdated_start_repoints_the_timer():
    api = _StubApi()
    skill = SleepSkill(_StubClient(api), TZ)
    started = datetime.now(TZ).replace(hour=5, minute=40, second=0, microsecond=0)

    result = await skill.execute({"action": "start", "start_time": started})

    assert api.started == ["child-1"]
    payload = api.doc.updates[0]
    assert payload["timer.timerStartTime"] == started.timestamp() * 1000
    assert payload["timer.timestamp"] == {"seconds": started.timestamp()}
    assert result.ok and "05:40" in result.message


async def test_plain_start_does_not_touch_the_timer_fields():
    api = _StubApi()
    result = await SleepSkill(_StubClient(api), TZ).execute({"action": "start"})
    assert api.started == ["child-1"] and api.doc.updates == []
    assert result.ok


# ---- backdated wake ---------------------------------------------------------

def _running_timer(started_at: datetime) -> dict:
    return {"timer": {"active": True, "timerStartTime": started_at.timestamp() * 1000}}


def test_sleep_wake_carries_an_explicit_time():
    skill = SleepSkill(None, TZ)
    args = skill.regex_parse("she's awake at 7:30", TZ)
    assert args["action"] == "complete"
    assert (args["end_time"].hour, args["end_time"].minute) == (7, 30)
    # A bare wake still stops the timer server-side, with no end time in the args.
    assert skill.regex_parse("she's awake", TZ) == {"action": "complete"}
    assert skill.coerce_llm({"action": "complete", "end_time": "now"}, TZ) == {"action": "complete"}
    assert skill.coerce_llm({"action": "complete", "end_time": "07:30"}, TZ)["end_time"].hour == 7


async def test_backdated_wake_arms_the_timer_end_then_completes():
    started = datetime.now(TZ).replace(hour=5, minute=40, second=0, microsecond=0)
    ended = started.replace(hour=7, minute=30)
    api = _StubApi(_running_timer(started))

    result = await SleepSkill(_StubClient(api), TZ).execute({"action": "complete", "end_time": ended})

    payload = api.doc.updates[0]
    assert payload["timer.timerEndTime"] == ended.timestamp() * 1000
    assert payload["timer.paused"] is True and payload["timer.active"] is True
    assert api.completed == ["child-1"]  # armed first, completed second
    assert result.ok and "07:30" in result.message


async def test_plain_wake_does_not_read_or_arm_the_timer():
    api = _StubApi(_running_timer(datetime.now(TZ)))
    result = await SleepSkill(_StubClient(api), TZ).execute({"action": "complete"})
    assert api.doc.updates == [] and api.completed == ["child-1"]
    assert result.ok


async def test_backdated_wake_with_no_timer_running_says_so():
    # complete_sleep returns quietly when nothing is running, so without the read-back this
    # would claim a sleep had been logged when none was.
    api = _StubApi({"timer": {"active": False}})
    result = await SleepSkill(_StubClient(api), TZ).execute(
        {"action": "complete", "end_time": datetime.now(TZ)}
    )
    assert api.completed == [] and api.doc.updates == []
    assert not result.verified and "No sleep timer is running" in result.message


async def test_backdated_wake_before_the_start_is_refused():
    started = datetime.now(TZ).replace(hour=8, minute=0, second=0, microsecond=0)
    api = _StubApi(_running_timer(started))

    result = await SleepSkill(_StubClient(api), TZ).execute(
        {"action": "complete", "end_time": started.replace(hour=7)}
    )

    assert api.completed == [] and api.doc.updates == []
    assert not result.verified and "started at 08:00" in result.message


# ---- nursing ----------------------------------------------------------------

def test_nursing_start_with_side():
    args = NursingSkill(None, TZ).regex_parse("feeding now on the left", TZ)
    assert args == {"action": "start", "side": "left"}


def test_nursing_complete():
    assert NursingSkill(None, TZ).regex_parse("done nursing", TZ) == {"action": "complete"}


# ---- growth -----------------------------------------------------------------

def test_growth_weight_and_height():
    args = GrowthSkill(None).regex_parse("55cm 5.1kg", TZ)
    assert args["weight"] == 5.1 and args["height"] == 55 and args["units"] == "metric"


def test_growth_head_circumference():
    args = GrowthSkill(None).regex_parse("head 38cm", TZ)
    assert args["head"] == 38 and args["height"] is None


# ---- regex fast-path priority ----------------------------------------------

def _first_match(text: str):
    for skill in _skills():
        if skill.regex_parse(text, TZ) is not None:
            return skill.name
    return None


def test_pump_wins_over_bottle_for_pumped_volume():
    # 'pumped 120ml' has a volume bottle would otherwise grab — pump is registered first.
    assert _first_match("pumped 120ml") == "huckleberry_pump"


def test_bottle_still_wins_plain_volume():
    assert _first_match("90ml formula") == "huckleberry_bottle"


def test_diaper_wins_poo():
    assert _first_match("big green poo") == "huckleberry_diaper"


def test_sleep_wins_down_now():
    assert _first_match("down now") == "huckleberry_sleep"
