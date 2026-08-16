"""WhatsApp Cloud API channel."""

from __future__ import annotations

import hashlib
import hmac
import logging

import aiohttp

from app.channels.base import InboundMessage

log = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
SEND_TIMEOUT = aiohttp.ClientTimeout(total=10)


class WhatsAppCloudChannel:
    name = "whatsapp"

    def __init__(self, phone_number_id: str, access_token: str, app_secret: str) -> None:
        self._url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"
        self._token = access_token
        self._app_secret = app_secret
        self._session: aiohttp.ClientSession | None = None

    # ---- inbound ------------------------------------------------------------

    def verify_signature(self, body: bytes, header: str | None) -> bool:
        if not self._app_secret:
            log.warning("WA_APP_SECRET not set — signature check DISABLED (dev only)")
            return True
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(self._app_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header.removeprefix("sha256="))

    def parse(self, payload: dict) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                # 'statuses' events (sent/delivered/read) have no 'messages' key.
                for msg in (change.get("value") or {}).get("messages") or []:
                    kind = msg.get("type", "unknown")
                    out.append(
                        InboundMessage(
                            id=msg.get("id", ""),
                            sender=msg.get("from", ""),
                            kind=kind,
                            text=(msg.get("text") or {}).get("body", "") if kind == "text" else "",
                        )
                    )
        return out

    # ---- outbound -----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=SEND_TIMEOUT)
        return self._session

    async def send_text(self, to: str, text: str) -> None:
        session = await self._get_session()
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        async with session.post(self._url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.error("send to %s failed %s: %s", to, resp.status, body)
            else:
                log.info("sent -> %s", to)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
