"""Spike B preflight — check the OUTBOUND half of the round trip on its own.

The full spike has two independent halves that fail for different reasons:

  outbound  your creds → Graph API → your phone      (token / phone number ID / recipient list)
  inbound   your phone → Meta → tunnel → webhook     (tunnel URL / verify token / field subscription)

Debugging both at once is miserable, so prove outbound first. If this script puts a message
on your phone, every later failure is an inbound problem and you can ignore the token.

Run:  uv run python scripts/wa_preflight.py
      uv run python scripts/wa_preflight.py --send      # actually sends you a message
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

load_dotenv()

PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
APP_SECRET = os.getenv("WA_APP_SECRET", "").strip()
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
ALLOWED = [n.strip() for n in os.getenv("ALLOWED_WA_IDS", "").split(",") if n.strip()]

API = "https://graph.facebook.com/v21.0"
TIMEOUT = aiohttp.ClientTimeout(total=15)

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def check_env() -> bool:
    """Catch the mistakes that produce confusing Graph API errors later."""
    print("\n-- .env --")
    ok = True

    for name, value in [
        ("WA_PHONE_NUMBER_ID", PHONE_NUMBER_ID),
        ("WA_ACCESS_TOKEN", ACCESS_TOKEN),
        ("WA_APP_SECRET", APP_SECRET),
        ("WA_VERIFY_TOKEN", VERIFY_TOKEN),
    ]:
        if value:
            shown = value if name == "WA_PHONE_NUMBER_ID" else f"set ({len(value)} chars)"
            print(f"[{OK}] {name:<20} {shown}")
        else:
            print(f"[{BAD}] {name:<20} missing")
            ok = False

    # The single most common copy/paste error: the phone number instead of its ID.
    if PHONE_NUMBER_ID.startswith("+") or (PHONE_NUMBER_ID and not PHONE_NUMBER_ID.isdigit()):
        print(f"[{BAD}] WA_PHONE_NUMBER_ID looks like a phone number, not an ID (a long integer)")
        ok = False

    if not ALLOWED:
        print(f"[{BAD}] {'ALLOWED_WA_IDS':<20} missing")
        return False

    # These must be E.164 with the '+' stripped — country code included, no trunk '0', no
    # separators. That is the exact form Meta puts in the webhook's `from` field, so a
    # mismatch here means the message arrives and is silently dropped by the allowlist.
    for n in ALLOWED:
        if not n.isdigit():
            print(f"[{BAD}] ALLOWED_WA_IDS {n!r}: digits only — strip '+', spaces, dashes, brackets")
            ok = False
        elif n.startswith("0"):
            print(f"[{BAD}] ALLOWED_WA_IDS {n!r}: starts with 0 — drop the national trunk '0' and")
            print(f"         prefix the country code instead (07700 900123 -> 447700900123)")
            ok = False
        elif len(n) < 8:
            print(f"[{BAD}] ALLOWED_WA_IDS {n!r}: too short ({len(n)} digits) — country code missing?")
            ok = False
        elif len(n) > 15:
            print(f"[{BAD}] ALLOWED_WA_IDS {n!r}: {len(n)} digits, E.164 allows at most 15")
            ok = False

    if len(ALLOWED) != len(set(ALLOWED)):
        print(f"[{WARN}] ALLOWED_WA_IDS has duplicates — harmless, but probably a paste error")

    # The test number caps its recipient list at 5, so anything beyond that cannot have been
    # registered with Meta and will fail to send with error 131030.
    if len(ALLOWED) > 5:
        print(f"[{BAD}] {len(ALLOWED)} recipients — the test number allows at most 5")
        ok = False
    elif ok:
        print(f"[{OK}] {'ALLOWED_WA_IDS':<20} {', '.join(ALLOWED)}")
        print(f"[{OK}] {'recipients':<20} {len(ALLOWED)} of 5 test-number slots used")

    return ok


async def check_number(session: aiohttp.ClientSession) -> bool:
    """Resolve the phone number ID. Proves the token is valid AND scoped to this number."""
    print("\n-- token + phone number ID --")
    url = f"{API}/{PHONE_NUMBER_ID}"
    params = {"fields": "display_phone_number,verified_name,quality_rating,platform_type"}
    async with session.get(url, params=params, headers=auth()) as r:
        body = await r.json()
    if r.status >= 400:
        err = body.get("error", {})
        print(f"[{BAD}] {r.status} {err.get('message', body)}")
        print(f"       code={err.get('code')} type={err.get('type')}")
        print(explain(err.get("code")))
        return False
    print(f"[{OK}] number    {body.get('display_phone_number')}  ({body.get('verified_name')})")
    print(f"[{OK}] platform  {body.get('platform_type', 'n/a')}  quality={body.get('quality_rating', 'n/a')}")
    return True


async def check_token_expiry(session: aiohttp.ClientSession) -> None:
    """A 24h dev token is the trap PLAN.md §3 warns about — catch it before it expires mid-build."""
    print("\n-- token lifetime --")
    async with session.get(f"{API}/debug_token", params={"input_token": ACCESS_TOKEN}, headers=auth()) as r:
        body = await r.json()
    data = body.get("data") or {}
    if r.status >= 400 or not data:
        # Needs an app token to introspect; not fatal, just uninformative.
        print(f"[{WARN}] could not introspect token — skipping (this check is optional)")
        return

    expires = data.get("expires_at")
    scopes = data.get("scopes", [])
    if expires == 0 or data.get("data_access_expires_at") == 0 or expires is None:
        print(f"[{OK}] never expires — this is a System User permanent token")
    else:
        print(f"[{BAD}] expires_at={expires} — this is a TEMPORARY token, it dies within 24h")
        print("       Generate a System User token instead: scripts/meta_setup.md step 5")

    for needed in ("whatsapp_business_messaging", "whatsapp_business_management"):
        mark = OK if needed in scopes else WARN
        print(f"[{mark}] scope {needed}")


async def send_probe(session: aiohttp.ClientSession, to: str) -> bool:
    """A free-form text send. Only works inside an open 24h service window (see below)."""
    print(f"\n-- send free-form text to {to} --")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": "preflight ok — outbound works"},
    }
    async with session.post(f"{API}/{PHONE_NUMBER_ID}/messages", json=payload, headers=auth()) as r:
        body = await r.json()
    if r.status >= 400:
        err = body.get("error", {})
        print(f"[{BAD}] {r.status} {err.get('message', body)}  code={err.get('code')}")
        print(explain(err.get("code")))
        return False
    wamid = (body.get("messages") or [{}])[0].get("id")
    print(f"[{OK}] accepted by Meta, wamid={wamid}")
    print("       Check your phone. Accepted ≠ delivered — if nothing arrives, the recipient")
    print("       is registered but the 24h service window is closed (message the bot first).")
    return True


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def explain(code: int | None) -> str:
    hints = {
        190: "       Token invalid or expired. Regenerate (meta_setup.md step 5).",
        131030: "       Recipient not in the test number's allowed list (meta_setup.md step 3).",
        131047: "       24h service window closed. Message the bot from your phone first, then retry.",
        131026: "       Undeliverable — number has no WhatsApp account, or wrong country code.",
        100: "       Bad parameter — usually WA_PHONE_NUMBER_ID is wrong or not owned by this token.",
        200: "       Missing permission — token lacks whatsapp_business_messaging.",
        133010: "       Phone number not registered with Cloud API.",
    }
    return hints.get(code, "       See https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", action="store_true", help="send a text to every number in ALLOWED_WA_IDS")
    ap.add_argument("--to", help="send to this one number instead (E.164 digits, no '+')")
    args = ap.parse_args()

    if not check_env():
        print("\nFix .env first — see scripts/meta_setup.md.")
        return 1

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        if not await check_number(session):
            return 1
        await check_token_expiry(session)

        if args.send:
            targets = [args.to] if args.to else ALLOWED
            results = [await send_probe(session, to) for to in targets]
            if not all(results):
                failed = [to for to, ok in zip(targets, results) if not ok]
                print(f"\n{len(failed)} of {len(targets)} recipients failed: {', '.join(failed)}")
                print("Most likely: registered in ALLOWED_WA_IDS but not in Meta's recipient")
                print("list (or vice versa). See meta_setup.md step 3.")
                return 1
            if len(targets) > 1:
                print(f"\nAll {len(targets)} recipients accepted.")
        else:
            print("\n-- send --")
            print(f"[{WARN}] skipped. Re-run with --send to message all {len(ALLOWED)} recipient(s).")

    print("\nOutbound half looks good. Next: tunnel + webhook (meta_setup.md steps 7-9).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
