"""LLM provider seam.

The router's third tier: when the regex fast-path can't parse a message, an LLM turns
freeform text into a tool call. Each skill publishes a neutral JSON tool schema
(`{name, description, parameters}`); the provider adapts it to its own wire format and
returns a `ToolCall`, or `None` if it couldn't decide on a tool.

Two implementations sit behind this, chosen at startup from `LLM_PROVIDER`:
  - NvidiaProvider   — free tier, OpenAI-compatible, via aiohttp
  - AnthropicProvider — paid, reliable, via the official `anthropic` SDK

Keeping the return type this small means nothing above the seam knows which model ran.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# What the LLM is told it's doing. Deliberately narrow: it is a parser, not a chat
# assistant (see PLAN.md §3 policy note — the LLM must stay auxiliary to a structured bot).
SYSTEM_PROMPT = (
    "You convert short baby-logging messages into a structured tool call. "
    "Pick the single tool that matches what the message describes and fill only the fields "
    "you are confident about. Never guess a value that isn't in the message: if the amount, "
    "unit, or contents aren't stated, leave that field out and the app will ask. "
    "In particular never assume millilitres vs ounces — they differ ~30x. "
    "If the message isn't something any tool can log, don't call a tool."
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def choose_tool(self, text: str, tools: list[dict[str, Any]]) -> ToolCall | None:
        """Return the tool the model picked for `text`, or None if it picked none."""
        ...

    async def close(self) -> None: ...


def build_provider(settings: Any) -> LLMProvider | None:
    """Construct the configured provider, or None to run regex-only.

    Returning None (rather than raising) is deliberate: a missing key or an unknown
    provider name degrades to the regex fast-path plus a polite "didn't understand" for
    the tail, which is strictly better than the app failing to start.
    """
    provider = (settings.llm_provider or "").strip().lower()

    if provider == "nvidia":
        if not settings.nvidia_api_key:
            log.warning("LLM_PROVIDER=nvidia but NVIDIA_API_KEY is empty — running regex-only")
            return None
        from app.llm.nvidia import NvidiaProvider

        return NvidiaProvider(
            api_key=settings.nvidia_api_key,
            model=settings.nvidia_model,
            base_url=settings.nvidia_base_url,
            reasoning=settings.nvidia_reasoning,
        )

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            log.warning("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty — running regex-only")
            return None
        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
        )

    if provider:
        log.warning("unknown LLM_PROVIDER=%r — running regex-only", provider)
    return None
