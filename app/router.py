"""Router: command -> regex fast path -> LLM fallback.

Skill-agnostic. Each skill owns its parsing, clarification, and execution (app/skills/base);
the router just walks skills and dispatches. Adding a capability touches no code here.
"""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from app.channels.base import Channel, InboundMessage
from app.journal import Journal
from app.llm.base import LLMProvider
from app.skills.base import Registry, Skill

log = logging.getLogger(__name__)

HELP = (
    "I log baby entries to Huckleberry. Just tell me what happened.\n\n"
    "Bottles:   11am 90ml breast milk · 90ml formula\n"
    "Nappies:   poo nappy · wet at 2pm · big green poo\n"
    "Sleep:     down now · awake · slept 2pm to 4pm\n"
    "           sleep start 5:40am · awake at 7:30  (either end can be backdated)\n"
    "Nursing:   feeding now left · finished nursing\n"
    "Pump:      pumped 120ml · expressed 3oz left\n"
    "Growth:    weight 5.2kg · 55cm 5.1kg\n\n"
    "Or just describe it in your own words and I'll work it out.\n\n"
    "Commands:  /help  /last  /retry\n"
    "Note: entries can't be deleted from here — use the Huckleberry app for that."
)

# Scratch keys a skill may attach for its own confirmation copy or to carry state across a
# clarifying question; never sent to execute. Names are skill-scoped enough not to collide
# with a real field (bottle's "amount" stays put).
_INTERNAL_KEYS = {"time_was_explicit", "source_text", "amount_hint", "amount_target"}


class Router:
    def __init__(
        self,
        registry: Registry,
        journal: Journal,
        channel: Channel,
        tz: ZoneInfo,
        llm: LLMProvider | None = None,
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._channel = channel
        self._tz = tz
        self._llm = llm

    async def handle(self, msg: InboundMessage) -> None:
        row_id = await self._journal.claim(msg.id, msg.sender, msg.text)
        if row_id is None:
            log.info("duplicate message %s — ignoring", msg.id)
            return

        if msg.kind != "text":
            await self._reply(msg.sender, f"I can only read text for now (got {msg.kind}).")
            await self._journal.mark_unparsed(row_id)
            return

        text = msg.text.strip()
        if not text:
            await self._journal.mark_unparsed(row_id)
            return

        if text.startswith("/"):
            await self._handle_command(msg, text, row_id)
            return

        # An outstanding clarifying question for THIS sender takes precedence, so a bare
        # "formula" is read as the answer rather than as a new entry.
        if await self._try_complete_pending(msg, text, row_id):
            return

        # Regex fast path, in registration order (keyword-specific skills before the
        # volume catch-all). The first skill to claim the message wins.
        for skill in self._registry.ordered():
            args = skill.regex_parse(text, self._tz)
            if args is not None:
                await self._dispatch(skill, msg, args, row_id)
                return

        # Third tier: hand the tail to the LLM (if configured).
        if self._llm is not None and await self._try_llm(msg, text, row_id):
            return

        await self._journal.mark_unparsed(row_id)
        await self._reply(
            msg.sender,
            "I didn't understand that. Try `11am 90ml breast milk`, or /help.",
        )

    # ---- commands -----------------------------------------------------------

    async def _handle_command(self, msg: InboundMessage, text: str, row_id: int) -> None:
        command = text.split()[0].lower().lstrip("/")

        if command in {"help", "start"}:
            await self._reply(msg.sender, HELP)
        elif command == "last":
            rows = await self._journal.recent(sender=msg.sender, limit=5)
            if not rows:
                await self._reply(msg.sender, "Nothing logged yet.")
            else:
                lines = [f"• {r['raw_text']}" for r in rows]
                await self._reply(msg.sender, "Your last entries:\n" + "\n".join(lines))
        elif command == "retry":
            failed = await self._journal.failed_entries()
            if not failed:
                await self._reply(msg.sender, "Nothing to retry.")
            else:
                await self._reply(
                    msg.sender,
                    f"{len(failed)} failed entr{'y' if len(failed) == 1 else 'ies'}. "
                    "Automatic retry lands in Phase 5; for now they're safe in the journal.",
                )
        else:
            await self._reply(msg.sender, f"Unknown command /{command}. Try /help.")

        await self._journal.mark_ok(row_id)

    # ---- clarification ------------------------------------------------------

    async def _try_complete_pending(self, msg: InboundMessage, text: str, row_id: int) -> bool:
        pending = await self._journal.get_pending(msg.sender)
        if pending is None:
            return False

        skill = self._registry.get(pending["skill"])
        if skill is None:
            await self._journal.clear_pending(msg.sender)
            return False

        updated = skill.complete_pending(dict(pending["args"]), text, self._tz)
        if updated is None:
            # Not an answer — drop the stale question and treat it as a new message.
            await self._journal.clear_pending(msg.sender)
            return False

        await self._journal.clear_pending(msg.sender)
        await self._dispatch(skill, msg, updated, row_id)
        return True

    # ---- llm ----------------------------------------------------------------

    async def _try_llm(self, msg: InboundMessage, text: str, row_id: int) -> bool:
        assert self._llm is not None
        try:
            call = await self._llm.choose_tool(text, self._registry.tool_schemas())
        except Exception:
            log.exception("LLM provider failed for message %s", msg.id)
            return False

        if call is None:
            return False
        skill = self._registry.get(call.name)
        if skill is None:
            log.info("LLM picked unregistered tool %r — ignoring", call.name)
            return False

        args = skill.coerce_llm(call.arguments, self._tz)
        await self._dispatch(skill, msg, args, row_id)
        return True

    # ---- dispatch -----------------------------------------------------------

    async def _dispatch(
        self, skill: Skill, msg: InboundMessage, args: dict[str, Any], row_id: int
    ) -> None:
        question = skill.missing_question(args)
        if question:
            await self._journal.set_pending(msg.sender, skill.name, args, question)
            await self._journal.mark_unparsed(row_id)
            await self._reply(msg.sender, question)
            return

        clean = {k: v for k, v in args.items() if k not in _INTERNAL_KEYS}
        await self._journal.set_parsed(row_id, skill.name, clean)
        result = await skill.execute(clean)

        if result.ok:
            await self._journal.mark_ok(row_id)
        else:
            await self._journal.mark_failed(row_id, result.error or "unknown")
        await self._reply(msg.sender, result.message)

    async def _reply(self, to: str, text: str) -> None:
        try:
            await self._channel.send_text(to, text)
        except Exception:
            log.exception("failed to send reply to %s", to)
