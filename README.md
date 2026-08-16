# WhatsApp Assistant

A WhatsApp bot that turns plain text (`11am 90ml breast milk`) into structured entries in
downstream systems. First integration: **Huckleberry** baby tracking.

See [PLAN.md](./PLAN.md) for architecture, phases, and risks.

**Status:** Phase 0 ✅ · Phase 1 ✅ · Phase 2 ✅ · Phase 3 ✅ (LLM fallback) · Phase 4 🔶 — six skills built (bottle, diaper, pump, sleep, nursing, growth), 89 tests green, live LLM routing verified; zero-cost deploy stack ready for an Oracle Always Free VM (see [deploy/oracle-setup.md](deploy/oracle-setup.md)).

## Run the bot

```bash
uv run uvicorn app.main:app --port 8000 --reload
cloudflared tunnel --url http://localhost:8000        # second terminal
```

Point the Meta webhook at `https://<tunnel-host>/webhook/whatsapp`, then message the test
number:

```
11am 90ml breast milk          → logs it, confirms
90ml formula                   → logs at the current time
2:30pm 120 ml breast milk
90ml                           → asks what was in it, then logs your answer
/help  /last  /retry
```

`GET /health` reports which credentials are configured without revealing them.

> **Built on [py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api) by
> [Woyken](https://github.com/Woyken)** (MIT). All Huckleberry protocol reverse-engineering is
> their work — see [CREDITS.md](./CREDITS.md).
>
> Not affiliated with Huckleberry Labs, Inc. or Meta Platforms, Inc. Uses an unofficial client
> that may break at any time. Personal use, your own account, at your own risk.

## Setup

```bash
uv sync                     # Python 3.14 + deps
cp .env.example .env        # then fill it in
```

## Phase 0 — run the two spikes

### Spike A: Huckleberry

Add `HUCKLEBERRY_EMAIL` / `HUCKLEBERRY_PASSWORD` / `LOCAL_TZ` to `.env`, then:

```bash
uv run python scripts/spike_huckleberry.py            # read-only: auth, list children, list feeds
uv run python scripts/spike_huckleberry.py --write    # logs ONE real 90ml entry, verifies by read-back
```

The `--write` run creates a real entry in your Huckleberry account. Delete it in the app
afterwards — the library has no delete method.

**Expected log noise:** every feed read prints
`Error fetching feed intervals: … FirebaseBottleFeedIntervalData.amount Field required`.
This is a known library bug (PLAN.md §9) caused by one historical amount-less bottle entry.
It has been **measured to lose zero entries** for recent windows — do not treat it as a new
failure.

To re-check that at any time:

```bash
uv run python scripts/diagnose_feeds.py --days 3
```

It reads raw Firestore with no validation, prints every entry in true chronological order,
flags any the library can't parse, and compares the raw count against what
`list_feed_intervals()` returns — so if the library ever does start losing entries, it says so
explicitly.

### Spike B: WhatsApp round trip

Follow [scripts/meta_setup.md](./scripts/meta_setup.md) to get your Meta credentials. Then
check the outbound half on its own, before any webhook exists:

```bash
uv run python scripts/wa_preflight.py --send    # should land a message on your phone
```

Once that passes, bring up the inbound half:

```bash
brew install cloudflared                                   # one-time
uv run uvicorn scripts.spike_whatsapp:app --port 8000 --reload
cloudflared tunnel --url http://localhost:8000             # second terminal
```

Point the Meta webhook at `https://<tunnel-host>/webhook/whatsapp` and subscribe to the
`messages` field — either in the dashboard, or without hunting for it:

```bash
uv run python scripts/wa_webhook.py --set https://<tunnel-host>/webhook/whatsapp
uv run python scripts/wa_webhook.py --show      # confirm callback URL + fields
```

Then message the test number. Expect `echo: <your text>` back.

**Exit criteria for Phase 0:** a real entry in Huckleberry, and a real echo in WhatsApp.

## Tests

```bash
uv run pytest
```

Covers the webhook's security-critical paths (signature verification, sender allowlist,
message dedupe) with no network calls.

## Notes

- This Mac's system Python is 3.9; the Huckleberry client requires 3.14. Always use `uv run`.
- `.env` is gitignored. Huckleberry auth is email + password, so treat it as sensitive.

## Credits

Built on **[py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api)** by
**[Woyken](https://github.com/Woyken)**, MIT licensed, used as an unmodified dependency.
The Huckleberry protocol work is theirs. See [CREDITS.md](./CREDITS.md) for full attribution,
licence text, and the disclaimer.

This project is MIT licensed — see [LICENSE](./LICENSE).

## Contributing back

This is a standalone project, not a fork. Bugs found in `py-huckleberry-api` while building it
are reported upstream rather than patched locally; see the table in [CREDITS.md](./CREDITS.md).
