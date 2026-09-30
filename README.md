# WhatsApp Assistant

Log baby-tracking entries by sending a WhatsApp message. The bot reads plain text such as
`11am 90ml breast milk`, works out what you meant, and writes the entry to
[Huckleberry](https://huckleberrycare.com), the baby-tracking app.

```
You:  11am 90ml breast milk
Bot:  Bottle logged — 90ml Breast Milk at 11:00.

You:  90ml
Bot:  What was in it? e.g. `breast milk`, `formula`, `cow milk`
You:  formula
Bot:  Bottle logged — 90ml Formula at 14:05.
```

It does one job: it turns one message into one logged entry and asks a question when something
is missing. It does not schedule, remind, remember past conversations, or answer questions
about the data.

## What you can log

| Entry | Example messages |
|---|---|
| Bottle | `11am 90ml breast milk` · `90ml formula` · `2:30pm 120 ml breast milk` |
| Diaper | `wet diaper` · `poo nappy` · `dirty diaper at 2pm` · `big green poo` |
| Sleep | `down now` · `awake` · `slept 2pm to 4pm` · `sleep start 5:40am` |
| Nursing | `feeding now left` · `finished nursing` |
| Pump | `pumped 120ml` · `expressed 3oz left` |
| Growth | `weight 5.2kg` · `55cm 5.1kg` |

Anything that doesn't match a known pattern goes to an LLM, which picks the entry type and
fills in the fields from free-form wording. Without an LLM key the bot runs on patterns alone.

Commands: `/help` lists the examples above, `/last` shows your recent entries, `/retry` counts
entries that failed to reach Huckleberry.

## How it works

```
WhatsApp message
  → Meta WhatsApp Cloud API (webhook)
  → FastAPI app
      1. verify Meta's signature, check the sender is on the allowlist, drop duplicates
      2. reply 200 immediately, process in the background
      3. route the message:
           slash command?        → handle it
           regex match?          → parse it (free, instant, works offline)
           otherwise             → LLM tool call picks the skill and fills the fields
      4. write the entry to a local SQLite journal first, then call Huckleberry
      5. read the entry back from Huckleberry where possible, then reply
  → confirmation to the sender
```

Each kind of entry is a **skill**: a class that owns its regex, its LLM tool schema, its
clarifying question and its Huckleberry call. The router knows nothing about babies. It walks
the registered skills in order and dispatches.

## Design decisions

The full reasoning is in [PLAN.md](./PLAN.md). The ones that shaped the code most:

- **Regex before LLM.** Most messages follow a handful of patterns. Regex handles those for
  free, in no time, and keeps working when the LLM provider is down. The LLM only sees the rest.
- **Ask, don't guess.** If a required field such as the amount is missing, the bot asks
  instead of inventing a value. A wrong feed volume is worse than one extra question.
- **Write-ahead journal.** Every entry is saved to SQLite before Huckleberry is called. The
  Huckleberry client is unofficial and can break without warning; the journal means an entry is
  never lost when it does.
- **Closed by default.** The webhook URL is public, so the app refuses to start without
  Meta's app secret and an allowlist of phone numbers, and ignores everyone else.
- **One household, several senders.** Everyone on the allowlist logs to the same Huckleberry
  account. Clarifying questions are tracked per sender, so one parent's answer can't complete
  the other parent's entry.
- **Swappable providers.** The LLM sits behind one interface with two implementations:
  NVIDIA (free tier, the default) and Anthropic Claude. Changing `LLM_PROVIDER` switches them.

## Limitations

- **Unofficial Huckleberry API.** Huckleberry has no public API. This uses
  [py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api), a reverse-engineered
  client that may break at any time.
- **No undo.** The client cannot delete entries, so mistakes are fixed in the Huckleberry app.
- **`/retry` does not replay yet.** It reports failed entries; replaying them is planned.
- **A pending question never expires.** If the bot asks "What was in it?" and you ignore it,
  your next message may be read as the answer.
- **Times without am/pm are read as 24-hour.** Write `2:30pm`, not `2:30`.

## What I learned

- **Deterministic first, LLM second.** I wanted every message to reach the right skill and be
  logged exactly as sent, so I moved as much as I could into regex. The LLM only sees what the
  patterns can't parse, which keeps calls down and results predictable.
- **Wiring the whole workflow.** Building this meant connecting WhatsApp to my own server,
  calling an externally hosted LLM (NVIDIA), and writing to a third-party app.
- **A message can replace an app.** It opened up ideas I had been turning over about fitting
  tools into my family's daily routine. I came away feeling that many household and personal
  apps could be replaced by a simple interaction like this one.

## Run it

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.14, which the Huckleberry
client needs), a Huckleberry account, and a Meta developer app with WhatsApp enabled.

```bash
uv sync
cp .env.example .env          # then fill in your credentials
uv run pytest                 # no network calls, no credentials needed
```

1. **Get the Meta credentials** by following [scripts/meta_setup.md](./scripts/meta_setup.md),
   then check that sending works: `uv run python scripts/wa_preflight.py --send`
2. **Check Huckleberry access** (read-only): `uv run python scripts/spike_huckleberry.py`
3. **Start the bot** and expose it with a tunnel:
   ```bash
   uv run uvicorn app.main:app --port 8000
   cloudflared tunnel --url http://localhost:8000              # second terminal
   uv run python scripts/wa_webhook.py --set https://<tunnel-host>/webhook/whatsapp
   ```
4. Message your WhatsApp test number. `GET /health` shows which credentials are set without
   revealing them.

**Deploying:** [deploy/oracle-setup.md](./deploy/oracle-setup.md) runs it for free on an Oracle
Cloud Always Free VM with Docker Compose, Caddy for HTTPS and DuckDNS for the hostname. After
that, `./deploy/push.sh` ships local changes in one command.

**Expected log noise:** feed reads print
`Error fetching feed intervals: … amount Field required`. It is a known bug in the Huckleberry
client, measured to lose no recent entries (PLAN.md §9).
`uv run python scripts/diagnose_feeds.py --days 3` re-checks this at any time.

## Project layout

```
app/
  main.py              FastAPI app: webhook, startup checks, skill registration order
  router.py            command → regex → LLM routing, clarifying questions, replies
  journal.py           SQLite write-ahead journal and message dedupe
  config.py            settings from .env
  channels/            messaging channel interface; WhatsApp Cloud API implementation
  llm/                 LLM provider interface; NVIDIA and Anthropic implementations
  parsers/             shared time and volume parsing
  skills/base.py       the Skill interface and registry
  skills/huckleberry/  one file per entry type: bottle, diaper, sleep, nursing, pump, growth
scripts/               setup and diagnostic tools (Meta setup, preflight, webhook, spikes)
deploy/                Oracle VM guide and push script
tests/                 unit and integration tests, all offline
PLAN.md                architecture, decisions, phases, risks, findings
```

**Adding a skill:** write a class that implements the `Skill` interface in
[app/skills/base.py](./app/skills/base.py), and register it in `app/main.py`. Registration
order is regex priority, so register specific skills before general ones (bottle, the volume
catch-all, goes last).

## Credits

Built on **[py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api)** by
**[Woyken](https://github.com/Woyken)** (MIT), used as an unmodified dependency. All the
Huckleberry protocol reverse-engineering is their work. Bugs found in it are reported upstream
rather than patched here. See [CREDITS.md](./CREDITS.md).

Not affiliated with Huckleberry Labs, Inc. or Meta Platforms, Inc. For personal use with your
own account, at your own risk.

MIT licensed. See [LICENSE](./LICENSE).
