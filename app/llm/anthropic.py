"""Anthropic provider — tool calling via the official `anthropic` SDK.

Uses the async SDK client rather than raw HTTP (the SDK is the supported path for Python).
`anthropic` is imported lazily so a NVIDIA-only deployment never needs the dependency
installed. The skill schema `{name, description, parameters}` maps onto Anthropic's
`{name, description, input_schema}` tool shape.

Default model is `claude-haiku-4-5`: this is a small, high-volume extraction task, exactly
what Haiku is for. Override with ANTHROPIC_MODEL to trade cost for capability.
"""

from __future__ import annotations

import logging
from typing import Any

from app.llm.base import SYSTEM_PROMPT, ToolCall

log = logging.getLogger(__name__)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "LLM_PROVIDER=anthropic needs the 'anthropic' package. "
                "Run `uv add anthropic` (or set LLM_PROVIDER=nvidia)."
            ) from exc

        self._model = model
        self._client = AsyncAnthropic(api_key=api_key)

    async def choose_tool(self, text: str, tools: list[dict[str, Any]]) -> ToolCall | None:
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": text}],
        )

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                return ToolCall(name=block.name, arguments=dict(args))
        return None

    async def close(self) -> None:
        await self._client.close()
