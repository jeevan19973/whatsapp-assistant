"""FastAPI entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response

from app.channels.whatsapp import WhatsAppCloudChannel
from app.config import settings
from app.journal import Journal
from app.llm.base import build_provider
from app.router import Router
from app.skills.base import Registry
from app.skills.huckleberry.bottle import BottleSkill
from app.skills.huckleberry.client import HuckleberryClient, install_log_filter
from app.skills.huckleberry.diaper import DiaperSkill
from app.skills.huckleberry.growth import GrowthSkill
from app.skills.huckleberry.nursing import NursingSkill
from app.skills.huckleberry.pump import PumpSkill
from app.skills.huckleberry.sleep import SleepSkill

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
log = logging.getLogger("app")


class State:
    journal: Journal
    channel: WhatsAppCloudChannel
    huckleberry: HuckleberryClient
    router: Router
    llm: object | None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_log_filter()

    # Fail closed. Without the app secret anyone can forge a webhook POST; without the
    # allowlist anyone who finds the number can write to the child's Huckleberry account.
    if missing := settings.missing_security():
        raise RuntimeError(f"refusing to start: set {', '.join(missing)} in .env")

    state.journal = Journal(settings.db_path)
    state.channel = WhatsAppCloudChannel(
        phone_number_id=settings.wa_phone_number_id,
        access_token=settings.wa_access_token,
        app_secret=settings.wa_app_secret,
    )
    state.huckleberry = HuckleberryClient(
        email=settings.huckleberry_email,
        password=settings.huckleberry_password,
        timezone=settings.local_tz,
        child_uid=settings.huckleberry_child_uid,
    )

    registry = Registry()
    # Registration order = regex fast-path priority. Keyword-specific skills first; bottle
    # (the volume catch-all) last, so 'pumped 120ml' isn't grabbed as a bottle.
    registry.register(DiaperSkill(state.huckleberry))
    registry.register(SleepSkill(state.huckleberry, settings.tz))
    registry.register(NursingSkill(state.huckleberry, settings.tz))
    registry.register(PumpSkill(state.huckleberry))
    registry.register(GrowthSkill(state.huckleberry))
    registry.register(BottleSkill(state.huckleberry))

    state.llm = build_provider(settings)

    state.router = Router(
        registry=registry,
        journal=state.journal,
        channel=state.channel,
        tz=settings.tz,
        llm=state.llm,
    )

    log.info(
        "ready — tz=%s skills=%s allowlist=%d db=%s llm=%s",
        settings.local_tz,
        registry.names(),
        len(settings.allowed),
        settings.db_path,
        getattr(state.llm, "name", "none (regex-only)"),
    )
    yield

    await state.channel.close()
    await state.huckleberry.close()
    if state.llm is not None:
        await state.llm.close()
    state.journal.close()


app = FastAPI(lifespan=lifespan, title="WhatsApp Assistant")


@app.get("/webhook/whatsapp")
async def verify(request: Request) -> Response:
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.wa_verify_token:
        log.info("webhook verification succeeded")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    log.warning("webhook verification FAILED — check WA_VERIFY_TOKEN")
    return Response(status_code=403)


@app.post("/webhook/whatsapp")
async def receive(request: Request, background: BackgroundTasks) -> Response:
    raw = await request.body()

    if not state.channel.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("rejected: bad signature")
        return Response(status_code=403)

    try:
        payload = await request.json()
    except Exception:
        log.warning("rejected: body was not JSON")
        return Response(status_code=400)

    for msg in state.channel.parse(payload):
        if msg.sender not in settings.allowed:
            log.warning("rejected: sender %s not in ALLOWED_WA_IDS", msg.sender)
            continue
        background.add_task(_guarded, msg)

    # 200 before any work runs: Meta retries on slow responses, and a retry is a duplicate.
    return Response(status_code=200)


async def _guarded(msg) -> None:
    """A background task that raises is invisible — log it rather than lose it."""
    try:
        await state.router.handle(msg)
    except Exception:
        log.exception("handler error for message %s", msg.id)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "phone_number_id_set": bool(settings.wa_phone_number_id),
        "token_set": bool(settings.wa_access_token),
        "app_secret_set": bool(settings.wa_app_secret),
        "huckleberry_configured": bool(settings.huckleberry_email and settings.huckleberry_password),
        "allowlist_size": len(settings.allowed),
        "tz": settings.local_tz,
        "llm": getattr(state, "llm", None) and getattr(state.llm, "name", None) or "regex-only",
    }
