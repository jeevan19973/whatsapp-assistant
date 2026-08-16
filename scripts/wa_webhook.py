"""Configure and inspect the WhatsApp webhook over the Graph API instead of the App Dashboard.

Two reasons this exists:

1. The dashboard's Configuration panel lives in one of two places depending on how the app was
   created (`WhatsApp -> Configuration`, or `Use cases -> Customize -> Configuration`), and is
   easy to fail to find.
2. The `cloudflared` quick-tunnel hostname changes on every restart, so the callback URL has to
   be re-pointed constantly during development. One command beats six clicks.

Run:  uv run python scripts/wa_webhook.py --show
      uv run python scripts/wa_webhook.py --set https://<tunnel-host>/webhook/whatsapp
      uv run python scripts/wa_webhook.py --subscribe-waba

TWO SUBSCRIPTIONS ARE REQUIRED. This cost hours to find, so it is worth stating plainly:

  /{app-id}/subscriptions      WHERE webhooks are delivered (callback URL, verify token,
                               fields). Set by --set.
  /{waba-id}/subscribed_apps   WHETHER that WABA emits events to your app at all.
                               Set by --subscribe-waba.

Configuring only the first gives you a successful verification handshake, `active: true`, the
`messages` field subscribed — and total silence, with nothing anywhere reporting a problem. A
fresh test WABA typically lists only Meta's own "WA DevX Webhook Events 1P App", not yours.
The dashboard does both at once, which is why this trap is invisible when clicking through it.

Tokens differ per endpoint, and they are not interchangeable:
  app access token (`<app-id>|<app-secret>`)  -> /{app-id}/subscriptions
  System User token (WA_ACCESS_TOKEN)         -> /{waba-id}/subscribed_apps

Needs WA_APP_ID + WA_APP_SECRET, and WA_WABA_ID for the WABA operations.

IMPORTANT: --set makes Meta immediately GET your callback URL to run the verification
handshake, so uvicorn and the tunnel must already be running or it fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.getenv("WA_APP_ID", "").strip()
APP_SECRET = os.getenv("WA_APP_SECRET", "").strip()
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
WABA_ID = os.getenv("WA_WABA_ID", "").strip()
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()

API = "https://graph.facebook.com/v21.0"
OBJECT = "whatsapp_business_account"
FIELDS = "messages"  # all this bot needs; add message_status etc. later if wanted
TIMEOUT = aiohttp.ClientTimeout(total=20)


def app_token() -> str:
    """App access token. Distinct from the System User token used to send messages."""
    return f"{APP_ID}|{APP_SECRET}"


def check_env(need_verify_token: bool) -> bool:
    missing = [n for n, v in [("WA_APP_ID", APP_ID), ("WA_APP_SECRET", APP_SECRET)] if not v]
    if need_verify_token and not VERIFY_TOKEN:
        missing.append("WA_VERIFY_TOKEN")
    if missing:
        print(f"Missing in .env: {', '.join(missing)}")
        if "WA_APP_ID" in missing:
            print("WA_APP_ID is on App Dashboard -> Settings -> Basic, beside the app secret.")
        return False
    return True


async def waba_apps(session: aiohttp.ClientSession) -> list[str]:
    """App ids the WABA currently emits webhooks to. Empty/foreign list = the silent failure."""
    async with session.get(
        f"{API}/{WABA_ID}/subscribed_apps", params={"access_token": ACCESS_TOKEN}
    ) as r:
        body = await r.json()
    if r.status >= 400:
        fail(r.status, body)
        return []
    ids = []
    for row in body.get("data", []):
        d = row.get("whatsapp_business_api_data", {})
        ids.append(str(d.get("id", "")))
        print(f"  subscribed app  {d.get('id')}  {d.get('name')}")
    if not ids:
        print("  (no apps subscribed at all)")
    return ids


async def show_waba(session: aiohttp.ClientSession) -> int:
    print(f"\nWABA {WABA_ID} subscribed_apps:")
    ids = await waba_apps(session)
    if APP_ID in ids:
        print(f"  -> your app {APP_ID} IS subscribed")
        return 0
    print(f"  -> your app {APP_ID} is NOT subscribed. No inbound message can ever arrive.")
    print("     Fix:  uv run python scripts/wa_webhook.py --subscribe-waba")
    return 1


async def subscribe_waba(session: aiohttp.ClientSession) -> int:
    """Subscribe the token's app to the WABA — the half the dashboard does invisibly."""
    print(f"Subscribing app {APP_ID} to WABA {WABA_ID}")
    async with session.post(
        f"{API}/{WABA_ID}/subscribed_apps", data={"access_token": ACCESS_TOKEN}
    ) as r:
        body = await r.json()
    if r.status >= 400:
        return fail(r.status, body)
    print(f"  response: {body}")
    return await show_waba(session)


async def show(session: aiohttp.ClientSession) -> int:
    async with session.get(
        f"{API}/{APP_ID}/subscriptions", params={"access_token": app_token()}
    ) as r:
        body = await r.json()

    if r.status >= 400:
        return fail(r.status, body)

    subs = [s for s in body.get("data", []) if s.get("object") == OBJECT]
    if not subs:
        print(f"No '{OBJECT}' subscription on app {APP_ID}.")
        print("Create one:  uv run python scripts/wa_webhook.py --set https://<host>/webhook/whatsapp")
        return 1

    for s in subs:
        print(f"object       {s.get('object')}")
        print(f"callback_url {s.get('callback_url')}")
        print(f"active       {s.get('active')}")
        fields = s.get("fields") or []
        # fields come back as dicts ({name, version}) or bare strings depending on API version.
        names = [f.get("name") if isinstance(f, dict) else f for f in fields]
        print(f"fields       {', '.join(names) or '(none)'}")
        if "messages" not in names:
            print("\nWARNING: not subscribed to 'messages' — no inbound message will ever arrive.")
            return 1
    return 0


async def set_url(session: aiohttp.ClientSession, callback_url: str) -> int:
    if not callback_url.startswith("https://"):
        print("Callback URL must be https:// — Meta rejects plain http.")
        return 1
    if not callback_url.rstrip("/").endswith("/webhook/whatsapp"):
        print(f"Note: {callback_url} does not end in /webhook/whatsapp — that is the route the")
        print("spike server serves. Continuing anyway in case you changed it.")

    print(f"Subscribing app {APP_ID} to '{OBJECT}' fields '{FIELDS}'")
    print(f"  callback: {callback_url}")
    print("Meta will now GET that URL to verify it — uvicorn and the tunnel must be up.\n")

    payload = {
        "object": OBJECT,
        "callback_url": callback_url,
        "verify_token": VERIFY_TOKEN,
        "fields": FIELDS,
        "access_token": app_token(),
    }
    async with session.post(f"{API}/{APP_ID}/subscriptions", data=payload) as r:
        body = await r.json()

    if r.status >= 400:
        return fail(r.status, body)

    print("Subscription saved. Verifying by reading it back:\n")
    return await show(session)


def fail(status: int, body: dict) -> int:
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    print(f"FAILED {status}: {err.get('message', body)}  code={code}")
    hints = {
        2200: "Meta could not verify the callback URL. Is the tunnel up and does\n"
              "       WA_VERIFY_TOKEN match what the server has loaded? Restart uvicorn after\n"
              "       editing .env — it reads the token at import time.",
        190: "Bad app access token — check WA_APP_ID and WA_APP_SECRET.",
        100: "Bad parameter, usually a wrong WA_APP_ID.",
        200: "Permission denied — the app secret must belong to WA_APP_ID.",
    }
    if code in hints:
        print(f"       {hints[code]}")
    return 1


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print both subscriptions")
    g.add_argument("--set", metavar="URL", help="set callback URL and subscribe to 'messages'")
    g.add_argument("--subscribe-waba", action="store_true",
                   help="subscribe your app to the WABA (needs WA_WABA_ID)")
    args = ap.parse_args()

    if not check_env(need_verify_token=bool(args.set)):
        return 1
    if (args.subscribe_waba or args.show) and not WABA_ID:
        if args.subscribe_waba:
            print("Missing WA_WABA_ID in .env — it is on the API Setup page, labelled")
            print("'WhatsApp Business Account ID' (a long integer, not the phone number ID).")
            return 1

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        if args.subscribe_waba:
            return await subscribe_waba(session)
        if args.set:
            rc = await set_url(session, args.set)
            # The callback URL is only half the job; surface the other half immediately.
            if rc == 0 and WABA_ID:
                rc = await show_waba(session)
            return rc
        rc = await show(session)
        if WABA_ID:
            rc = await show_waba(session) or rc
        else:
            print("\n(set WA_WABA_ID in .env to also check /{waba-id}/subscribed_apps —")
            print(" without that subscription no inbound message is ever delivered)")
        return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
