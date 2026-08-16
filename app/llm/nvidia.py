"""NVIDIA provider — OpenAI-compatible chat completions with tool calling.

build.nvidia.com speaks the OpenAI wire format, and there is no Anthropic SDK for it, so
this talks to it directly with aiohttp (the same dependency the WhatsApp channel already
uses). The skill schema `{name, description, parameters}` maps straight onto OpenAI's
`{"type": "function", "function": {...}}` shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from app.llm.base import SYSTEM_PROMPT, ToolCall

log = logging.getLogger(__name__)


class NvidiaProvider:
    name = "nvidia"

    def __init__(self, api_key: str, model: str, base_url: str, reasoning: bool = False) -> None:
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._reasoning = reasoning
        self._session: aiohttp.ClientSession | None = None

    def _system(self) -> str:
        # Nemotron reasons by default; `/no_think` turns it off. Tool calling works either
        # way (model card). Prepend the control only for Nemotron so the prompt stays clean
        # for other models where the token is meaningless.
        if not self._reasoning and "nemotron" in self._model.lower():
            return "/no_think\n" + SYSTEM_PROMPT
        return SYSTEM_PROMPT

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def choose_tool(self, text: str, tools: list[dict[str, Any]]) -> ToolCall | None:
        payload = {
            "model": self._model,
            # Greedy for reasoning-off extraction; NVIDIA recommends 0.6 with reasoning on.
            "temperature": 0.6 if self._reasoning else 0,
            # Reasoning on emits a chain-of-thought before the tool call — leave headroom.
            "max_tokens": 2048 if self._reasoning else 512,
            "messages": [
                {"role": "system", "content": self._system()},
                {"role": "user", "content": text},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ],
            "tool_choice": "auto",
        }

        session = await self._http()
        async with session.post(self._url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("nvidia %s: %s", resp.status, body[:300])
                return None
            data = await resp.json()

        try:
            calls = data["choices"][0]["message"].get("tool_calls") or []
        except (KeyError, IndexError, TypeError):
            log.warning("nvidia response missing choices/message: %s", str(data)[:300])
            return None
        if not calls:
            return None

        fn = calls[0].get("function", {})
        name = fn.get("name")
        if not name:
            return None
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError):
            log.warning("nvidia tool arguments weren't valid JSON: %r", raw_args)
            return None

        return ToolCall(name=name, arguments=args)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
