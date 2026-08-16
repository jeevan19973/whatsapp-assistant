"""LLM tier tests — a fake provider drives the router's third tier. No network."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from app.channels.base import InboundMessage
from app.journal import Journal
from app.llm.base import ToolCall, build_provider
from app.router import Router
from app.skills.base import Registry, SkillResult
from app.skills.huckleberry.bottle import BottleSkill

TZ = ZoneInfo("America/New_York")
ALICE = "447700900123"


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, text: str) -> None:
        self.sent.append((to, text))


class FakeBottleSkill(BottleSkill):
    """Real parsing/coercion from BottleSkill; execute is faked (records args, no network)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, args: dict[str, Any]) -> SkillResult:
        self.calls.append(args)
        return SkillResult(ok=True, message="logged", verified=True)


class FakeProvider:
    """Returns a canned ToolCall, or raises, on demand."""

    name = "fake"

    def __init__(self, result: ToolCall | None = None, boom: bool = False) -> None:
        self.result = result
        self.boom = boom
        self.seen_tools: list[dict] | None = None

    async def choose_tool(self, text: str, tools: list[dict]) -> ToolCall | None:
        self.seen_tools = tools
        if self.boom:
            raise RuntimeError("provider down")
        return self.result

    async def close(self) -> None:
        pass


def make(provider):
    journal = Journal(":memory:")
    channel = FakeChannel()
    skill = FakeBottleSkill()
    registry = Registry()
    registry.register(skill)
    router = Router(registry=registry, journal=journal, channel=channel, tz=TZ, llm=provider)
    return router, channel, skill, journal


def msg(text: str, mid: str = "wamid.1") -> InboundMessage:
    return InboundMessage(id=mid, sender=ALICE, kind="text", text=text)


# ---- the third tier fires only when regex fails ----------------------------

async def test_regex_hit_never_reaches_the_llm():
    provider = FakeProvider(ToolCall("huckleberry_bottle", {"amount": 5}))
    router, _, skill, _ = make(provider)
    await router.handle(msg("11am 90ml breast milk"))
    # Regex handled it — provider was never consulted.
    assert provider.seen_tools is None
    assert skill.calls[0]["amount"] == 90


async def test_freeform_message_is_parsed_by_the_llm():
    call = ToolCall(
        "huckleberry_bottle",
        {"amount": 90, "units": "ml", "bottle_type": "Breast Milk", "time": "11am"},
    )
    # No volume, milk-type word, or "bottle" — the regex returns None, so the LLM runs.
    router, channel, skill, _ = make(FakeProvider(call))
    await router.handle(msg("she had her usual feed around 11 this morning"))

    assert len(skill.calls) == 1
    logged = skill.calls[0]
    assert logged["amount"] == 90
    assert logged["units"] == "ml"
    assert logged["bottle_type"] == "Breast Milk"
    assert logged["start_time"].hour == 11
    assert "time_was_explicit" not in logged  # internal flag stripped before dispatch


async def test_llm_registry_schemas_are_passed_through():
    router, _, _, _ = make(FakeProvider(None))
    await router.handle(msg("something unparseable"))
    provider = router._llm  # type: ignore[attr-defined]
    assert provider.seen_tools == [FakeBottleSkill.tool_schema]


# ---- incomplete extractions reuse the clarify flow -------------------------

async def test_partial_llm_extraction_asks_instead_of_guessing():
    # Model got the type but not the amount — must ask, not invent a volume.
    call = ToolCall("huckleberry_bottle", {"bottle_type": "Formula", "time": "now"})
    router, channel, skill, journal = make(FakeProvider(call))
    await router.handle(msg("just gave her a formula bottle"))

    assert skill.calls == []
    assert "How much?" in channel.sent[0][1]
    assert await journal.get_pending(ALICE) is not None


async def test_llm_never_guesses_units():
    # A bare number with no unit stays MISSING — ml vs oz differ ~30x.
    call = ToolCall("huckleberry_bottle", {"amount": 90, "bottle_type": "Formula", "time": "now"})
    router, channel, skill, _ = make(FakeProvider(call))
    await router.handle(msg("90 of formula"))
    assert skill.calls == []
    assert "ml or oz" in channel.sent[0][1]


# ---- graceful degradation --------------------------------------------------

async def test_provider_error_falls_back_to_didnt_understand():
    router, channel, skill, _ = make(FakeProvider(boom=True))
    await router.handle(msg("gibberish the regex can't handle"))
    assert skill.calls == []
    assert "didn't understand" in channel.sent[0][1]


async def test_no_tool_chosen_falls_back():
    router, channel, skill, _ = make(FakeProvider(None))
    await router.handle(msg("what's the weather like"))
    assert skill.calls == []
    assert "didn't understand" in channel.sent[0][1]


async def test_unknown_tool_name_is_ignored():
    router, channel, skill, _ = make(FakeProvider(ToolCall("some_future_skill", {})))
    await router.handle(msg("log my expenses"))
    assert skill.calls == []
    assert "didn't understand" in channel.sent[0][1]


async def test_no_provider_configured_is_regex_only():
    router, channel, skill, _ = make(None)
    await router.handle(msg("freeform with no provider"))
    assert skill.calls == []
    assert "didn't understand" in channel.sent[0][1]


# ---- build_provider factory ------------------------------------------------

class _Settings:
    llm_provider = "nvidia"
    nvidia_api_key = ""
    nvidia_model = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    nvidia_base_url = "https://example/v1"
    nvidia_reasoning = False
    anthropic_api_key = ""
    anthropic_model = "claude-haiku-4-5"


def test_build_provider_returns_none_without_a_key():
    assert build_provider(_Settings()) is None


def test_build_provider_unknown_name_returns_none():
    s = _Settings()
    s.llm_provider = "banana"
    assert build_provider(s) is None


def test_build_provider_builds_nvidia_with_a_key():
    s = _Settings()
    s.nvidia_api_key = "nvapi-test"
    provider = build_provider(s)
    assert provider is not None and provider.name == "nvidia"


def test_nemotron_reasoning_off_prepends_no_think():
    from app.llm.nvidia import NvidiaProvider

    off = NvidiaProvider("k", "nvidia/llama-3.3-nemotron-super-49b-v1.5", "https://x/v1", reasoning=False)
    assert off._system().startswith("/no_think")

    on = NvidiaProvider("k", "nvidia/llama-3.3-nemotron-super-49b-v1.5", "https://x/v1", reasoning=True)
    assert not on._system().startswith("/no_think")

    # Non-Nemotron models never get the control token.
    other = NvidiaProvider("k", "meta/llama-3.3-70b-instruct", "https://x/v1", reasoning=False)
    assert not other._system().startswith("/no_think")
