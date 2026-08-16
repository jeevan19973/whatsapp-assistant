"""Spike: prove the LLM tier works end-to-end without WhatsApp or a tunnel.

Builds the configured provider from .env and runs a handful of freeform messages — the kind
the regex fast-path can't parse — straight through `choose_tool`, printing the tool call and
extracted arguments. This isolates the LLM integration: is the model id valid, does
tool-calling work, do the arguments come back parseable?

    uv run python scripts/spike_llm.py

Needs a key for whichever LLM_PROVIDER is set (NVIDIA_API_KEY or ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Running `python scripts/spike_llm.py` puts scripts/ on the path, not the project root,
# so `app` isn't importable. Add the root (unlike the other spikes, this one imports app).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.llm.base import build_provider
from app.skills.huckleberry.bottle import BottleSkill
from app.skills.huckleberry.diaper import DiaperSkill
from app.skills.huckleberry.growth import GrowthSkill
from app.skills.huckleberry.nursing import NursingSkill
from app.skills.huckleberry.pump import PumpSkill
from app.skills.huckleberry.sleep import SleepSkill

# The full tool set the LLM chooses from — one schema per skill.
TOOLS = [
    s.tool_schema
    for s in (BottleSkill, DiaperSkill, SleepSkill, NursingSkill, PumpSkill, GrowthSkill)
]

# Freeform phrasings across every skill — the tail the deterministic parser can't handle.
# Each should resolve to the right tool (or none for the last one).
PHRASES = [
    "gave her about 90 of breast milk around 11 this morning",
    "topped him up with three ounces of formula just now",
    "huge nappy explosion, mustard yellow, around 3",
    "she finally went down for her nap",
    "just latched her on the right side",
    "managed to express about 100ml this morning",
    "weighed her today, she's 5 and a half kilos",
    "what's the weather like today",  # not loggable — expect no tool call
]


async def main() -> None:
    provider = build_provider(settings)
    if provider is None:
        raise SystemExit(
            f"No LLM provider built (LLM_PROVIDER={settings.llm_provider!r}). "
            "Set the matching API key in .env."
        )

    print(f"provider: {provider.name}\n")

    try:
        for phrase in PHRASES:
            call = await provider.choose_tool(phrase, TOOLS)
            print(f"  {phrase!r}")
            if call is None:
                print("    -> no tool call\n")
            else:
                print(f"    -> {call.name} {call.arguments}\n")
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
