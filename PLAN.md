# WhatsApp Personal Assistant — Implementation Plan

**Created:** 2026-08-03

**Status:**
- ✅ **Phase 0** — Huckleberry writes + reads verified; WhatsApp round trip verified.
- ✅ **Phase 1** — skeleton: config, channel seam, signature verify, allowlist, durable dedupe,
  fast-200-then-background, `/help`.
- ✅ **Phase 2** — bottle vertical slice built and unit/integration tested (58 tests green).
  Real-device gate closed: a real feed logged end-to-end from both registered phones,
  each verified `ok` in the journal (write + read-back).
- ✅ **Phase 3** — LLM fallback (71 tests green): `LLMProvider` seam with NVIDIA + Anthropic
  implementations, tool schemas generated from the skill registry, router third tier reusing
  the clarify/pending flow. Live gate closed: real Nemotron inference via `scripts/spike_llm.py`
  extracts amount/units/type/time from freeform messages (times normalized to HH:MM by the
  model, anchored by `timeparse`). Degrades to regex-only when no key is set.
- 🔶 **Phase 4** — skill host generalized (router is now skill-agnostic; each skill owns its
  regex parse, LLM coercion, missing-field question, and clarify-answer). Five skills added:
  diaper (full detail), pump, sleep (live timer + intervals), nursing (live timer + intervals),
  growth. 89 tests green; live LLM routing verified across all six skills via spike_llm.
  Deploy artifacts built for **Oracle Cloud Always Free** (zero cost, chosen over Fly — see §4):
  Dockerfile (Python 3.14), docker-compose (app + Caddy HTTPS + DuckDNS), deploy/oracle-setup.md.
  Remaining: provision the VM and run the stack; real-device write verification lands naturally
  once it's live.

## 1. What we're building

A WhatsApp bot that acts as a personal logging assistant. You send it plain text
(`11am 90ml breast milk`), it figures out what you meant, and it writes the entry to the
right downstream system. First downstream system is **Huckleberry** (baby tracking) via the
unofficial [`Woyken/py-huckleberry-api`](https://github.com/Woyken/py-huckleberry-api).

Design goal that outranks everything else: **adding a new capability later must be a
one-file change.** The baby-tracking skills are the first tenant of a general skill host,
not the point of the system.

## 2. Architecture

```
You (WhatsApp)
   │
   ▼
Meta WhatsApp Cloud API
   │  POST (webhook)
   ▼
┌──────────────────────────────────────────────────────────┐
│ FastAPI app                                              │
│                                                          │
│  POST /webhook/whatsapp                                  │
│    ├─ verify X-Hub-Signature-256                         │
│    ├─ allowlist check (any id in ALLOWED_WA_IDS)         │
│    ├─ dedupe on message.id                               │
│    └─ return 200 fast, process in background task        │
│                                                          │
│  Channel adapter  ──► InboundMessage (normalized)        │
│         │            (WhatsAppCloudChannel | TelegramChannel)
│         ▼                                                │
│  Router                                                  │
│    1. slash command?  (/help /undo /last /today)         │
│    2. regex fast-path (deterministic, free, instant)     │
│    3. LLM adapter with tool-calling  ──► Intent          │
│         │            (NvidiaProvider | AnthropicProvider)│
│         ▼                                                │
│  Journal (SQLite)  ◄── write intent BEFORE dispatch      │
│         │                                                │
│         ▼                                                │
│  Skill registry ──► skill.execute(intent)                │
│         │                                                │
│         ├─ huckleberry_bottle / _sleep / _nursing / ...  │
│         └─ (future skills)                               │
│         │                                                │
│         ▼                                                │
│  Reply via channel adapter (confirmation + undo hint)    │
└──────────────────────────────────────────────────────────┘
   │
   ▼
Huckleberry (Firebase Firestore over gRPC)
```

### Two abstraction seams (deliberate)

| Seam | Protocol | Implementations |
|---|---|---|
| Messaging channel | `Channel` | `WhatsAppCloudChannel` (now), `TelegramChannel` (fallback / future) |
| LLM | `LLMProvider` | `NvidiaProvider` (free tier), `AnthropicProvider` (paid, reliable) |

Both are chosen at startup from env vars. Nothing above these seams knows which
implementation is live. This is what makes "start on the test number, migrate later" cheap.

### Key design decisions

1. **Write-ahead journal.** Every parsed intent is committed to local SQLite *before*
   calling Huckleberry, with a status of `pending → ok | failed`. Rationale: the Huckleberry
   client is reverse-engineered and can break without warning. If it breaks you must never
   lose the entry — you replay from the journal. Bonus: the journal becomes the data source
   for the future dashboard, so the dashboard never has to read back out of Huckleberry.

2. **Regex before LLM.** ~90% of real messages will match a handful of patterns. Those cost
   nothing, take ~0ms, and work when the LLM provider is down. The LLM only handles the tail.

3. **Tool-calling for the LLM path.** Each skill publishes a JSON tool schema. The model picks
   a tool and fills arguments. Adding a skill = adding a schema, not editing a prompt.

4. **Confirm, don't guess.** If a required field is missing or confidence is low, the bot asks
   (using WhatsApp interactive buttons where possible) rather than inventing a value. Logging a
   wrong feed volume is worse than asking one question.

5. **Allowlist by default.** The webhook rejects any sender not in `ALLOWED_WA_IDS`. A public
   webhook URL is discoverable; the bot must be inert to everyone else.

6. **Multi-sender, single household.** Several people (both parents, family) are registered and
   all have equal rights to log. They write to **one shared Huckleberry account** — which is
   already how Huckleberry works, so no per-user credentials and no account routing. The design
   consequences are small but must be honoured from Phase 2, not retrofitted:
   - The journal stores the sender's WA id on every row. Without it you cannot answer "who
     logged this?" later, and the column is free to add now and painful to backfill.
   - **Confirmations go to the sender only.** The other parent is not notified. This is the only
     option that always works: a reply is only deliverable inside *that person's* own 24-hour
     service window, so messaging someone who has been quiet needs a paid approved template.
   - Dedupe stays keyed on Meta's `message.id`, which is globally unique, so it needs no
     per-sender scoping.
   - Conversation state (a pending clarifying question, per decision 4) must be keyed **per
     sender**. A single global "awaiting answer" slot would let one person's `120ml` answer a
     question the bot asked the other person.

7. **Timezone anchoring.** `11am` is resolved against a configured `LOCAL_TZ`. If the resulting
   time is in the future, assume it meant yesterday. All Huckleberry calls pass explicit
   timezone-aware `datetime` objects (the library takes `start_time`/`end_time` directly).

## 3. Channel: Cloud API test number now, real number later

Confirmed from research:

- A Meta app gives you a **free test phone number** and up to **5 registered recipient
  numbers**. Register a phone per person who should be able to log — you, your partner, family.
  No business verification, no cost, no spare SIM.
- **Registering a recipient is manual and dashboard-only.** There is no Graph API for the test
  number's recipient list. Each number is added in the dashboard and verified by an OTP sent to
  *that* phone, so its owner has to be present. Removing a number frees its slot. 5 is a hard
  cap — a production WABA lifts it, but that means business verification.
- **Two lists must agree:** Meta's recipient list *and* `ALLOWED_WA_IDS`. Missing from Meta's
  list → send fails with `131030` and that person's inbound messages never arrive at all.
  Missing from `ALLOWED_WA_IDS` → the message reaches the webhook and is silently dropped.
  `scripts/wa_preflight.py --send` messages every id in `ALLOWED_WA_IDS`, which is the cheapest
  way to prove the two lists match.
- **Inbound-initiated conversations are free.** You message the bot → a 24-hour service window
  opens → the bot can send unlimited free-form replies at no charge. Service conversations have
  been free since Nov 2024. Since you always initiate, ongoing cost is effectively **$0**.
  The window is **per sender**, not per bot: replying to whoever just messaged is always free,
  but reaching a second person who has been quiet is not possible without a paid template. This
  is why confirmations go to the sender only (decision 6).
- The dev-panel token **expires in 24 hours**. You need a **System User permanent token** from
  Meta Business Suite. Do this on day one or you will fight it repeatedly.
- **Do not register your spare number yet.** A number attached to the Cloud API cannot be used
  in the consumer WhatsApp or WhatsApp Business app — you'd have to delete its account first.
  That's a one-way door; take it only once the bot has proven itself.

### Policy note

Since **Jan 15, 2026** Meta prohibits *general-purpose AI assistants* on the WhatsApp Business
Platform (this is what removed ChatGPT / Perplexity / Copilot from WhatsApp). Structured bots
where AI is *auxiliary* to the function remain allowed. This project is a structured logging
bot and the LLM is an internal parser — squarely in the allowed category. The only future
exposure is if the "personal assistant" grows into open-domain chat **on a verified production
WABA**. Staying on the test number keeps the whole thing inside the dev sandbox regardless.

**Rejected: unofficial libraries** (Baileys, `whatsapp-web.js`). They QR-link a real account and
reverse-engineer the protocol; reported survival is 2–8 weeks before a permanent number ban.
Not worth it when the official path is free.

## 4. Hosting recommendation: Fly.io

Reasoning, since this was left open:

| Option | Verdict |
|---|---|
| **Fly.io** (recommended) | One always-on `shared-cpu-1x` machine, ~$2–3/mo. Deploys from a Dockerfile so Python 3.14 is trivial. **Keeps the Huckleberry Firebase session warm** and enables the library's real-time listeners later. No cold starts. |
| Cloud Run | Free tier is attractive, but scale-to-zero means nearly every message is a cold start → container boot **plus** a fresh Huckleberry email/password authentication (~1–3s) on almost every entry. That's a worse experience and hammers the reverse-engineered auth endpoint. Pinning `min-instances=1` to fix it costs *more* than Fly. |
| Local + Cloudflare Tunnel | **Use this during development** — stable public HTTPS URL for the webhook, instant iteration, $0. Not the production answer since the bot dies when your Mac sleeps. |

So: **Cloudflare Tunnel for phases 0–3, Fly.io from phase 4 on.** The app is a plain container
either way, so this is not a rewrite.

**Decision (updated, Phase 4): Oracle Cloud "Always Free" VM instead of Fly.** Goal was strictly
zero ongoing cost (Huckleberry is already a paid subscription). An Always Free Ampere VM keeps the
whole design intact — always-on (warm Huckleberry session), persistent disk (the SQLite journal),
and the fast-200-then-background pattern — none of which survive a serverless free tier. Firebase
was considered and rejected: its Python compute is Cloud Functions/Cloud Run, which is
scale-to-zero (cold-start auth on nearly every message), freezes CPU after the response (breaking
background processing), and has an ephemeral filesystem (would force a Firestore rewrite of the
journal). The tradeoff taken: we self-manage a small VM + HTTPS (Caddy + a free DuckDNS hostname)
instead of paying ~$2–3/mo for Fly's managed platform. Artifacts: `Dockerfile`,
`docker-compose.yml`, `Caddyfile`, `deploy/oracle-setup.md`.

## 5. Repo layout

```
WhatsApp-Assistant/
├── PLAN.md
├── README.md
├── pyproject.toml           # uv, requires-python = ">=3.14"
├── Dockerfile
├── fly.toml
├── .env.example
├── app/
│   ├── main.py              # FastAPI, webhook routes
│   ├── config.py            # env → typed settings
│   ├── channels/
│   │   ├── base.py          # Channel protocol, InboundMessage, OutboundMessage
│   │   ├── whatsapp.py      # signature verify, send, interactive buttons
│   │   └── telegram.py      # (optional fallback)
│   ├── llm/
│   │   ├── base.py          # LLMProvider protocol
│   │   ├── nvidia.py        # OpenAI-compatible, build.nvidia.com
│   │   └── anthropic.py
│   ├── router.py            # commands → regex → LLM
│   ├── parsers/
│   │   └── patterns.py      # regex fast-path + time resolution
│   ├── skills/
│   │   ├── base.py          # Skill protocol + registry + tool schemas
│   │   └── huckleberry/
│   │       ├── client.py    # session lifecycle, child_uid cache
│   │       ├── bottle.py
│   │       ├── sleep.py
│   │       ├── nursing.py
│   │       ├── diaper.py
│   │       └── growth.py
│   └── journal.py           # SQLite write-ahead log
├── scripts/
│   ├── spike_huckleberry.py
│   ├── diagnose_feeds.py     # tolerant raw-Firestore read, quantifies the library's truncation
│   ├── spike_whatsapp.py     # Spike B echo server
│   ├── wa_preflight.py       # outbound-half check: token, scopes, all recipients
│   ├── wa_webhook.py         # set/inspect the webhook subscription via Graph API
│   └── meta_setup.md
└── tests/
```

## 6. Environment variables

```
# Channel
CHANNEL=whatsapp
WA_PHONE_NUMBER_ID=
WA_ACCESS_TOKEN=            # System User permanent token
WA_APP_SECRET=              # for X-Hub-Signature-256 verification
WA_VERIFY_TOKEN=            # your own string, for the webhook GET handshake
ALLOWED_WA_IDS=            # comma-separated, E.164 without '+'; one per person who can log.
                           # Must match Meta's recipient list exactly. Max 5 on the test number.
WA_DISPLAY_NAMES=          # optional, id:Name pairs — 447700900123:Jeevan,447700900456:Sam
                           # Only used to make /today readable; unmapped ids fall back to the id

# LLM
LLM_PROVIDER=nvidia         # nvidia | anthropic
NVIDIA_API_KEY=
ANTHROPIC_API_KEY=

# Huckleberry
HUCKLEBERRY_EMAIL=
HUCKLEBERRY_PASSWORD=
HUCKLEBERRY_CHILD_UID=      # optional; auto-discovered if blank

# General
LOCAL_TZ=Europe/London
DB_PATH=/data/journal.db
```

Secrets live in `fly secrets` / a local `.env` that is gitignored. Never committed.

## 7. Phases

### Phase 0 — De-risk with two independent spikes (~half a day)

Do these *before* writing any application structure. Either one failing changes the plan.

- **Spike A — Huckleberry.** `scripts/spike_huckleberry.py`: authenticate, `get_user()`, print
  `childList`, then `log_bottle` a real 90ml breast milk entry at a past timestamp. Confirm it
  appears in the Huckleberry app. This is the single biggest unknown — it's a reverse-engineered
  client and it either works against the current backend or it doesn't.
  Note: needs Python 3.14 (`uv python install 3.14`); this machine has 3.9.
- **Spike B — WhatsApp round trip.** Create the Meta app, get the test number, register **every
  person's phone** as a recipient (`scripts/meta_setup.md` step 3), generate the permanent System
  User token, then prove the two halves separately: `scripts/wa_preflight.py --send` for outbound
  (token, scopes, all recipients), then `scripts/spike_whatsapp.py` behind a Cloudflare Tunnel
  for inbound. Debugging both halves at once is the main time sink here.

**Exit criteria:** a real entry in Huckleberry, and a real echo in WhatsApp — from **each**
registered phone, since a number missing from either list fails silently in a different way.

### Phase 1 — Skeleton (~half a day)

Project scaffolding, config, `Channel` protocol + `WhatsAppCloudChannel`, signature
verification, multi-sender allowlist, message dedupe, fast-200-then-process-in-background.
`InboundMessage` carries the sender id through to everything downstream — that is the one
thing that is expensive to add later. `/help` responds.

### Phase 2 — First vertical slice (~1 day)

Regex parser for the canonical form, the skill registry, and `huckleberry_bottle` wired
end-to-end plus the SQLite journal. Journal rows include a `sender_wa_id` column from the
first migration (decision 6) — cheap now, a backfill you cannot do later.

Target inputs:
```
11am 90ml breast milk
90ml formula                → now
2:30pm 120 ml breast milk
```
Reply (to the sender only): `✅ Bottle logged — 90ml breast milk at 11:00.`
No undo hint — see §9, the library has no delete method.

**Exit criteria:** each registered person can log a real feed from their own phone, and the
journal shows who logged what.

### Phase 3 — LLM fallback (~1 day)

`LLMProvider` protocol, both implementations, tool schemas generated from the skill registry,
and the router's third tier. Unparseable input now works:
```
gave her about 90 of breast milk around 11 this morning
she went down at 7 and woke up twice
```
Low-confidence or incomplete extractions trigger a clarifying question, not a guess. Pending
questions are keyed per sender (decision 6) so two people can be mid-clarification at once
without one answering the other's question.

### Phase 4 — Remaining skills + deploy to Fly (~1–2 days)

`log_sleep` / `start_sleep` / `complete_sleep`, `log_nursing`, `log_diaper`, `log_pump`,
`log_growth`. Dockerfile, `fly.toml`, persistent volume for SQLite, secrets, point the Meta
webhook at the Fly URL.

### Phase 5 — Polish (~1 day)

- `undo` (see risk below), `/last`, `/today` summary. `/today` covers the **whole household**,
  not just the asker — that is how the other parent catches up on entries they weren't told
  about, and it's the reason sender-only confirmations are acceptable. Attribute each line to
  who logged it (from `sender_wa_id`), mapped to a display name
- WhatsApp interactive buttons for clarification
- Graceful degradation: if Huckleberry fails, the journal entry stays `pending`, the reply says
  so honestly, and a `/retry` command replays pending entries
- Structured logging

### Phase 6 — Future (not scoped now)

- Dashboard reading from the journal DB
- Proactive nudges — these are *business-initiated*, so they require **approved message
  templates** and fall outside the free service window. Budget for a few cents.
- Non-baby skills: expenses, reminders, notes. Each is one file in `skills/`.

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Huckleberry client is reverse-engineered and may break | High | Write-ahead journal means no data loss; phase 0 spike proves writes work today; the journal is the real source of truth |
| Library read path aborts on one malformed entry (see §9) | **Low — measured zero loss** | Recent entries live in regular docs fetched by the query that completes first. Verify writes by exact match, not list length. Revisit only for historical/dashboard queries |
| Library's models are stricter than Huckleberry's real schema, so future entries could break reads more severely | Medium | `diagnose_feeds.py` quantifies loss on demand; write-ahead journal keeps our own copy regardless |
| **`undo` is NOT possible — confirmed** (see §10) | Medium | `undo` marks the journal row voided and replies telling you to delete in the app. Replies must not promise undo |
| Huckleberry auth is email + password, stored as a secret | Medium | Host secret store only, never in git; the account is a family account so treat it as sensitive |
| Python 3.14 requirement | Low | `uv` handles it; Dockerfile pins a 3.14 base image |
| Meta token / webhook config drift | Low | Permanent System User token from day one; document the exact steps in `scripts/meta_setup.md` |
| A recipient is in one list but not the other, so their messages vanish silently | Medium | `wa_preflight.py --send` messages every id in `ALLOWED_WA_IDS` and reports per-number failures; Phase 0 exit criteria require an echo from *each* phone |
| 5-recipient cap on the test number | Low | Enough for a household. Growing past it means a production WABA and business verification — the `Channel` seam keeps that a config change |
| Anyone on the allowlist can log anything, with no per-person permissions | Accepted | Deliberate: a household of adults sharing one baby-tracking account. Revisit only if a non-trusted number is ever added |
| Test number could be rotated by Meta | Low | Channel seam makes migration to a real WABA (or Telegram) a config change |
| NVIDIA free credits run out (~1,000 inferences, 40 rpm) | Low | Provider adapter — flip `LLM_PROVIDER=anthropic`. Regex fast-path means most messages never hit an LLM anyway |

## 9. Phase 0 findings (from inspecting the installed library)

`huckleberry-api` is installed and its real API surface inspected — 43 methods. Two findings
change the plan:

### No delete methods exist — `undo` is off the table

Confirmed: there is no `delete_*` or `remove_*` method anywhere on `HuckleberryAPI`. All
`log_*` methods also return `None`, so there is no entry ID to reference later either.
`cancel_sleep` / `cancel_nursing` only abort an *in-progress timer*, not a logged entry.

**Consequence:** `undo` becomes "mark voided in our journal + tell you to delete it in the
app". The bot must never claim it can undo. Phase 2 confirmation copy changes to:
`✅ Bottle logged — 90ml breast milk at 11:00.` with no undo hint.

### The library's read path is unsafe — it silently truncates (found by running Spike A)

Spike A's write **succeeded**, but the read-back logged:

```
Error fetching feed intervals: 4 validation errors for FirebaseFeedMultiContainer
data.1780557447344-….FirebaseBottleFeedIntervalData.amount   Field required
```

Root cause, traced through `api.py:2094-2140`:

1. Huckleberry stores feed entries two ways: **regular docs** (one per entry) and **multi
   containers** — batched archive docs holding many entries in a `data` dict. The app compacts
   older entries into containers over time.
2. `list_feed_intervals` runs two queries inside **one shared `try/except`**:
   - Query 1: regular docs, date-filtered in Firestore, `order_by("start")`.
   - Query 2: **every** multi container, with *no date filter* (you can't filter a nested
     field), validated whole, then filtered by date in Python.
3. `except (GoogleAPICallError, ValidationError)` sits around **both** queries and only logs.
   So one unparseable entry aborts the remaining iteration and the method
   **returns a partial list with no error raised**.

**The bad entry is a bottle feed with no `amount`.** `FirebaseBottleFeedIntervalData` declares
`amount: Number` as required, but Huckleberry clearly permits amount-less bottle entries
(`mode='bottle'`, `bottleType='Formula'`, `units` present, `amount` absent). The library's model
is stricter than the real schema. Genuine upstream bug, and it fires on **every** feed read,
because Query 2 fetches that container regardless of the date window.

### …but measured impact is currently ZERO — the mechanism is real, the blast radius is small

`diagnose_feeds.py` quantified it:

```
raw scan found  : 19 entries
library returned: 19 entries
match — no loss for this window
entries the library cannot parse, ANY date: 1
  mt6WB5rScmDlaGghzVFj / 1780557447344-…: missing amount, lastSide; wrong mode='bottle'
```

**No data is being lost.** My earlier "poisons every read" framing overstated the consequence.
Why the loss is nil:

- Huckleberry keeps **recent** entries as regular docs and only compacts **old** entries into
  multi containers. So Query 1 — which runs first, is date-filtered, and is `order_by("start")`
  — returns everything in any recent window.
- The exception hits during Query 2, *after* Query 1 has already appended its results. Query 2
  had no in-window entries to contribute, so aborting it cost nothing.

Two corrections to my earlier conclusions that follow from this:

1. **The returned list IS correctly sorted for recent windows**, because everything in it came
   from `order_by("start")`. My "not globally sorted" warning only applies to windows old
   enough to reach into multi containers. (The exact-match verification is still the right
   approach — it just isn't urgent.)
2. **The one malformed entry is a single historical record from 2026-06-04**, not a recurring
   pattern. One amount-less bottle entry in the whole account.

**Residual risk, stated precisely:** reads that reach far enough back to include
multi-container entries will silently drop some of them, and because Firestore `.stream()`
ordering is unspecified, *which* ones depends on whether the poisoned container is streamed
first. That bites the **future dashboard** (§Phase 6) and historical `/today`-style queries over
old data. It does not bite write-verification or same-day summaries.

**Decisions, revised after measurement:**

| | Decision |
|---|---|
| Writes | Keep using the library. Worked first time. |
| Reads (recent windows) | **The library is fine.** Proven equal to a raw scan. Don't pre-emptively reimplement — that would be speculative work against a bug with zero current impact. |
| Reads (historical) | Needs the tolerant reader, but **defer to Phase 6** when the dashboard actually needs it. Design note recorded, no code yet. |
| Write verification | Match on exact `(mode, start, amount)` rather than list length — cheap, already implemented, and immune to truncation whenever it does occur. |
| Log noise | The `Error fetching feed intervals` line will print on every read. Expect it; don't treat it as a new failure. Worth suppressing/downgrading in our own logging so it doesn't mask real errors. |
| Upstream | Worth filing on `Woyken/py-huckleberry-api`: make bottle `amount` optional, and move the `try/except` inside the per-document loop so one bad doc can't abort the batch. Low effort, benefits everyone. |

The write-ahead journal (§2, decision 1) stays in the design, but on this evidence it's
insurance against future schema drift rather than a fix for a live problem.

`scripts/diagnose_feeds.py` is kept as a permanent diagnostic: re-run it if reads ever look
wrong, and it will immediately say whether the library is losing entries.

### Read methods exist and are richer than the README suggested — use them

Undocumented in the README but present:
`list_feed_intervals`, `list_sleep_intervals`, `list_diaper_intervals`,
`list_pump_intervals`, `list_activity_intervals`, `list_health_entries`,
`get_child`, `get_latest_growth` — all taking `(child_uid, start_time, end_time)`.

Two uses, both upgrades to the plan. The library's reads are measured-safe for recent windows,
so use them directly; only historical queries need the tolerant reader (deferred to Phase 6):
1. **Write verification.** Since `log_*` returns `None`, a silent failure is otherwise
   invisible. Read back after writing and match the exact entry before confirming success.
2. **`/today` reads real data** from Huckleberry rather than only our own journal, so entries
   logged in the app — by anyone in the household — also show up. Huckleberry is the shared
   source of truth here; the journal only adds *who used the bot to log it*, which Huckleberry
   does not record. So `/today` reads Huckleberry for the entries and joins the journal for
   attribution, leaving app-logged entries simply unattributed.

If it's ever needed, the two queries involved (regular docs by `start` range, plus multi
containers) are ~40 lines against the raw Firestore client — `diagnose_feeds.py` already
contains a working version to lift from.

### Exact enum values (from `firebase_types.py`) — the parser must map to these

```
BottleType      "Breast Milk" | "Formula" | "Tube Feeding" | "Cow Milk" | "Goat Milk" | "Soy Milk" | "Other"
VolumeUnits     "ml" | "oz"
DiaperMode      "pee" | "poo" | "both" | "dry"
PooColor        "yellow" | "brown" | "black" | "green" | "red" | "gray"
PooConsistency  "solid" | "loose" | "runny" | "mucousy" | "hard" | "pebbles" | "diarrhea"
FeedSide        "left" | "right" | "none"
ActivityMode    "bath" | "tummyTime" | "storyTime" | "screenTime" | "skinToSkin" |
                "outdoorPlay" | "indoorPlay" | "brushTeeth"
PottyResult     "satButDry" | "wentPotty" | "accident"
pee/poo_amount  "little" | "medium" | "big"
```

Note `log_bottle` defaults `bottle_type="Formula"` — the parser must always pass this
explicitly rather than relying on the default.

Also: `log_diaper` takes `pee_amount` / `poo_amount` (not in the README), and there's a
`log_activity` method for bath / tummy time etc. that the README didn't mention — free extra
skills in Phase 4.

### Environment

- Python 3.14.4 installed via `uv`; all deps resolve cleanly on it (this Mac's system
  Python is 3.9, so always use `uv run`).
- Auth is Firebase `signInWithPassword` against project `simpleintervals`, with a token
  refresh endpoint — consistent with the README's claim, and it means credentials are sent
  to Google's identity toolkit, not to a third party.

## 10. Repository, licensing, and attribution

**Decision: a new standalone repo, not a fork — plus a separate throwaway fork used to submit
our bug fixes upstream.** Crediting is right; a fork is the wrong mechanism for it.

### Why not a fork

A GitHub fork declares "this is a derivative copy of that codebase." That isn't what this is:

- **We share zero lines of code with them.** We install `huckleberry-api` from PyPI as a
  dependency. Nothing of theirs is vendored or modified.
- A fork inherits their commit history, README, package name, and issue templates, and GitHub
  permanently badges the repo *"forked from Woyken/py-huckleberry-api"*. New PRs default to
  targeting **their** repo, which is a real footgun.
- Forks are excluded from GitHub search by default and can't be used as templates — so it
  actively hides the project.
- Practically: their repo is one Python client library; ours is a WhatsApp bot, a router, an LLM
  adapter layer, and a skill host. Ours will diverge to ~0% overlap.

### Licensing — checked, and it's the easy case

`huckleberry-api` 0.4.3 is **MIT** (author: Woyken). That means:

- **No copyleft obligation.** MIT does not reach through a dependency to our code, so we're free
  to license our project however we like. (Had it been GPL/AGPL, merely importing it would have
  raised real questions — worth having checked rather than assumed.)
- We are **not redistributing their source**, so we aren't strictly required to ship their
  licence text either. Attribution here is courtesy and good practice, not a legal duty.
- **Use MIT for our repo too.** It matches upstream, imposes nothing on anyone, and is the
  obvious default for a personal tool.

### How we actually give credit — three concrete things

1. **A prominent Credits section in our README**, naming the project, its author, and its
   licence, linking to the repo — and stating plainly that all Huckleberry protocol
   reverse-engineering is their work, not ours. That is the substantive part.
2. **Mirror their unofficial-status disclaimer.** They state they are not affiliated with
   Huckleberry Labs Inc. We inherit that relationship exactly and must say so, both to be
   honest and to avoid implying an endorsement that doesn't exist.
3. **Contribute the fixes back.** Better credit than a badge: improve the thing they built.

### Upstream contribution — likely to land, based on their issue history

Our finding (§9) is that `FirebaseBottleFeedIntervalData.amount` is declared required but real
Huckleberry data omits it. Checking their issues shows this is the **third instance of an
already-accepted bug class**:

| Issue | Status | Substance |
|---|---|---|
| #20 | **Closed/fixed** | `list_health_entries` fails because `HealthDataEntry.lastUpdated` is required but absent in real data |
| #25 | **Closed/fixed** | `leftDuration`/`rightDuration` should be optional in `FirebaseBreastFeedIntervalData` |
| *ours* | unreported | `amount` should be optional in `FirebaseBottleFeedIntervalData` |

Identical pattern, already twice accepted. So the PR is low-risk and worth filing. Two changes:

- Make bottle `amount` optional (the minimal fix they've precedent for).
- **Move the `try/except` inside the per-document loop** in `list_feed_intervals` so one bad
  document skips itself instead of aborting the remaining query. This is the more valuable fix —
  it makes the whole class of bug non-fatal in future — but it's a behaviour change, so propose
  it as a separate discussion rather than bundling it.

We're on 0.4.3, the latest release (2026-05-18), so there's no upgrade that already fixes this.

Note: issue **#43 "add contributing.md?"** is open, so there's no contribution guide — open an
issue describing the bug before sending a PR, rather than assuming a workflow.

### Also spotted upstream, relevant to Phase 4

Open issue **#51**: `complete_nursing` writes a *millisecond* `feedStartTime` into the interval
`start` field when the session was started by the iOS app — a seconds-vs-milliseconds bug. If we
implement nursing **timers** (`start_nursing` / `complete_nursing`) rather than only
`log_nursing`, we may hit this. Prefer `log_nursing` with explicit start/end where possible, and
sanity-check any timestamp that implies a date after ~1990 in seconds terms.

### Repo hygiene before the first push

- `.env` and `*.db` are already gitignored — verify with `git status` before the initial commit.
  A leaked Huckleberry email/password is full access to a family account.
- Never commit journal databases: they contain your children's feed and sleep records.
- `ALLOWED_WA_IDS` contains family phone numbers — keep it in `.env` only, never in
  `.env.example`, committed tests, or fixtures. The existing tests use the dummy
  `447700900123`, which is in Ofcom's reserved-for-fiction range; keep it that way.
- **Public or private?** Public is reasonable — the code is ours, MIT is compatible, and their
  own reverse-engineering repo is public. Private is the safer choice if you'd rather not
  advertise automated access to Huckleberry. Nothing in the design depends on which; decide
  before the first push, since making a repo private later doesn't unpublish what was cloned.

## 11. Open questions

- Is there exactly one child on the Huckleberry account, or does the bot need to disambiguate?
  Now slightly sharper with multiple senders: if there are two children, does each sender have a
  different default child, or is it one shared default for everyone?
- Do you want the bot to double as the *only* logging path, or will you keep using the
  Huckleberry app in parallel? (Affects how much the journal needs to reconcile.)
- Preferred confirmation verbosity — terse `✅ 90ml @ 11:00` or full sentences?
- Which numbers, and what display names for `/today` attribution?

### Settled

- **Multiple senders, one shared Huckleberry account, equal rights to log** — see decision 6.
- **Confirmations go to the sender only.** Others catch up via `/today`. Chosen because
  reaching a quiet recipient requires a paid template, making broadcast unreliable by design.
