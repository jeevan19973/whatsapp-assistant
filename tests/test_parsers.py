"""Parser tests. No network, no Huckleberry."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.parsers.bottle import MISSING, parse
from app.parsers.timeparse import resolve

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 3, 15, 30, tzinfo=TZ)  # Mon 3 Aug 2026, 15:30 local


# ---- time resolution -------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("11am", (2026, 8, 3, 11, 0)),
        ("11 am", (2026, 8, 3, 11, 0)),
        ("2:30pm", (2026, 8, 3, 14, 30)),
        ("14:00", (2026, 8, 3, 14, 0)),
        ("2.30 pm", (2026, 8, 3, 14, 30)),
        ("12am", (2026, 8, 3, 0, 0)),
        ("12pm", (2026, 8, 3, 12, 0)),
        ("now", (2026, 8, 3, 15, 30)),
    ],
)
def test_resolve_times(text, expected):
    dt, _ = resolve(text, TZ, NOW)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == expected


def test_future_time_rolls_back_a_day():
    """11pm typed at 15:30 hasn't happened yet today, so it means last night."""
    dt, _ = resolve("11pm", TZ, NOW)
    assert (dt.day, dt.hour) == (2, 23)


def test_no_time_returns_none():
    dt, remainder = resolve("90ml breast milk", TZ, NOW)
    assert dt is None
    assert remainder == "90ml breast milk"


def test_time_token_is_stripped():
    _, remainder = resolve("11am 90ml breast milk", TZ, NOW)
    assert "11am" not in remainder
    assert "90ml" in remainder


# ---- bottle parsing --------------------------------------------------------

def test_canonical_form():
    args = parse("11am 90ml breast milk", TZ, NOW)
    assert args["amount"] == 90
    assert args["units"] == "ml"
    assert args["bottle_type"] == "Breast Milk"
    assert args["start_time"].hour == 11
    assert args["time_was_explicit"] is True


def test_no_time_defaults_to_now():
    args = parse("90ml formula", TZ, NOW)
    assert args["bottle_type"] == "Formula"
    assert args["start_time"] == NOW.replace(second=0, microsecond=0)
    assert args["time_was_explicit"] is False


def test_spaced_units_and_pm_time():
    args = parse("2:30pm 120 ml breast milk", TZ, NOW)
    assert args["amount"] == 120
    assert args["start_time"].hour == 14
    assert args["start_time"].minute == 30


def test_word_order_does_not_matter():
    args = parse("breast milk 90ml at 11am", TZ, NOW)
    assert args["amount"] == 90
    assert args["bottle_type"] == "Breast Milk"
    assert args["start_time"].hour == 11


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90ml breastmilk", "Breast Milk"),
        ("90ml ebm", "Breast Milk"),
        ("90ml bm", "Breast Milk"),
        ("90ml formula", "Formula"),
        ("90ml cow milk", "Cow Milk"),
        ("90ml cow's milk", "Cow Milk"),
        ("90ml soy milk", "Soy Milk"),
        ("90ml goat milk", "Goat Milk"),
    ],
)
def test_bottle_type_synonyms(text, expected):
    assert parse(text, TZ, NOW)["bottle_type"] == expected


def test_oz_is_detected():
    args = parse("3oz formula", TZ, NOW)
    assert args["amount"] == 3
    assert args["units"] == "oz"


def test_decimal_amount():
    assert parse("2.5oz formula", TZ, NOW)["amount"] == 2.5


def test_bare_number_never_assumes_ml():
    """ml and oz differ ~30x — guessing would silently corrupt the log."""
    args = parse("90 formula", TZ, NOW)
    assert args["amount"] == 90
    assert args["units"] == MISSING


def test_missing_bottle_type_is_flagged_not_defaulted():
    """log_bottle defaults to Formula; relying on that would mislabel breast milk."""
    args = parse("90ml", TZ, NOW)
    assert args["bottle_type"] == MISSING


def test_bottle_word_alone_is_recognised():
    args = parse("bottle", TZ, NOW)
    assert args is not None
    assert args["amount"] == MISSING


@pytest.mark.parametrize("text", ["hello", "how are you", "", "slept 2 hours"])
def test_non_bottle_text_returns_none(text):
    assert parse(text, TZ, NOW) is None


def test_zero_amount_rejected():
    assert parse("0ml formula", TZ, NOW) is None
