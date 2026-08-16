"""Phase 0, Spike B — prove the WhatsApp Cloud API round trip works.

An echo server: you message the bot, it replies with what you said. Includes the
signature verification and sender allowlist from the start, because retrofitting
those onto a public webhook is how bots get abused.

Run:  uv run uvicorn scripts.spike_whatsapp:app --port 8000 --reload
Then: cloudflared tunnel --url http://localhost:8000
      and point the Meta webhook at  https://<tunnel-host>/webhook/whatsapp

See scripts/meta_setup.md for the exact Meta dashboard steps.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

import aiohttp
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response

load_dotenv()

# Log to a file as well as the console. During Spike B you are juggling several terminals
# (uvicorn, cloudflared, a shell), and "did the webhook fire?" is the single question you ask
# most often. A file answers it without needing to have been watching the right window.
LOG_PATH = os.getenv("SPIKE_LOG", "spike.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("spike")

PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
APP_SECRET = os.getenv("WA_APP_SECRET", "")
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
ALLOWED = {n.strip() for n in os.getenv("ALLOWED_WA_IDS", "").split(",") if n.strip()}

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
SEND_TIMEOUT = aiohttp.ClientTimeout(total=10)

app = FastAPI()
_seen_message_ids: set[str] = set()


def signature_ok(body: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256. Without this, anyone who finds the URL can drive the bot."""
    if not APP_SECRET:
        log.warning("WA_APP_SECRET not set — skipping signature check (dev only)")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def send_text(to: str, text: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    async with aiohttp.ClientSession(timeout=SEND_TIMEOUT) as session:
        async with session.post(GRAPH_URL, json=payload, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                log.error("send failed %s: %s", resp.status, body)
            else:
                log.info("sent -> %s", to)


@app.get("/webhook/whatsapp")
async def verify(request: Request) -> Response:
    """Meta's one-time subscription handshake."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        log.info("webhook verification succeeded")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    log.warning("webhook verification FAILED — check WA_VERIFY_TOKEN matches the dashboard")
    return Response(status_code=403)


async def _guarded(msg: dict) -> None:
    """A background task that raises is invisible — log it instead of losing it."""
    try:
        await handle_message(msg)
    except Exception:
        log.exception("handler error")


@app.post("/webhook/whatsapp")
async def receive(request: Request, background: BackgroundTasks) -> Response:
    raw = await request.body()

    if not signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("rejected: bad signature")
        return Response(status_code=403)

    payload = await request.json()
    log.info("inbound: %s", payload)

    # 200 goes out before any handling runs. Meta retries aggressively on slow or failed
    # responses, and a retry would duplicate every entry once real skills are wired up.
    # Dedupe still guards that, but not being slow is the first line of defence.
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                background.add_task(_guarded, msg)

    return Response(status_code=200)


async def handle_message(msg: dict) -> None:
    msg_id = msg.get("id", "")
    sender = msg.get("from", "")

    if msg_id in _seen_message_ids:
        log.info("duplicate %s — ignoring", msg_id)
        return
    _seen_message_ids.add(msg_id)

    if ALLOWED and sender not in ALLOWED:
        log.warning("rejected: sender %s not in ALLOWED_WA_IDS", sender)
        return

    if msg.get("type") != "text":
        await send_text(sender, f"Spike only handles text, got '{msg.get('type')}'.")
        return

    text = msg["text"]["body"]
    log.info("from %s: %r", sender, text)
    await send_text(sender, f"echo: {text}")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "phone_number_id_set": bool(PHONE_NUMBER_ID),
        "token_set": bool(ACCESS_TOKEN),
        "app_secret_set": bool(APP_SECRET),
        "allowlist_size": len(ALLOWED),
    }
