"""Skill seam — adding a capability is adding one of these plus a registry entry.

A Skill owns everything about its own vertical: the tool schema the LLM sees, the regex
fast-path, how it turns LLM/regex output into execute-ready args, what it asks when a
required field is missing, and how it completes a pending clarification. The router knows
none of that — it just walks skills and dispatches. That's what keeps "add a capability"
a one-file change (PLAN.md §2, the design goal that outranks everything else).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class SkillResult:
    ok: bool
    message: str          # user-facing reply
    verified: bool = False  # did we confirm the write landed downstream?
    error: str | None = None


@runtime_checkable
class Skill(Protocol):
    name: str
    # A neutral JSON tool schema: {"name", "description", "parameters": <json schema>}.
    # The LLM tier turns this into each provider's own tool format.
    tool_schema: dict[str, Any]

    def regex_parse(self, text: str, tz: ZoneInfo) -> dict[str, Any] | None:
        """Deterministic fast path. Return args (possibly with MISSING fields), or None if
        this message isn't for this skill. Keep it conservative — a false match is worse
        than falling through to the LLM."""
        ...

    def coerce_llm(self, raw: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        """Map a provider's tool-call arguments into the same arg shape regex_parse yields."""
        ...

    def missing_question(self, args: dict[str, Any]) -> str | None:
        """The question to ask if a required field is absent, or None if ready to execute."""
        ...

    def complete_pending(
        self, args: dict[str, Any], text: str, tz: ZoneInfo
    ) -> dict[str, Any] | None:
        """Fill the pending args from a freeform answer. Return updated args, or None if the
        text isn't an answer to the outstanding question (so it's treated as a new message)."""
        ...

    async def execute(self, args: dict[str, Any]) -> SkillResult: ...


class Registry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def ordered(self) -> list[Skill]:
        """Skills in registration order — the priority the regex fast-path walks. Register
        keyword-specific skills before the volume catch-all (bottle) so a 'pumped 120ml'
        isn't grabbed as a bottle."""
        return list(self._skills.values())

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Every skill's tool schema, for the LLM tier. Sorted so the prompt (and its cache
        key) doesn't churn on registration order."""
        return [self._skills[name].tool_schema for name in sorted(self._skills)]
