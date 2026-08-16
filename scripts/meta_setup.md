# Meta / WhatsApp Cloud API setup (Spike B)

Manual browser steps — nobody can automate these for you. ~20 minutes.
Fill each value into `.env` as you go.

## 1. Create the app

1. Go to <https://developers.facebook.com/apps> → **Create app**.
2. Use case: **Other** → app type: **Business** → name it (e.g. `personal-assistant`).
3. If prompted for a Business Portfolio, create one. A personal portfolio is fine at this
   stage — you are **not** doing business verification yet.

## 2. Add WhatsApp

1. On the app dashboard → **Add product** → **WhatsApp** → **Set up**.
2. Meta auto-creates a test WhatsApp Business Account and a **free test phone number**.
3. Copy the **Phone number ID** (a long integer, *not* the phone number itself) → `WA_PHONE_NUMBER_ID`.

## 3. Register recipient phone numbers

Only registered numbers can exchange messages with the test number, in **either** direction.
The cap is **5**, and there is no Graph API for this list — it is dashboard-only, so adding a
number is always a manual step.

Still on the WhatsApp → **API Setup** page:

1. Under **To**, click **Manage phone number list**.
2. **Add phone number** → enter it in E.164 *with* the `+` (e.g. `+447700900123`).
3. WhatsApp sends an OTP to **that** phone; whoever holds it enters the code. You cannot verify
   someone else's number from your own device — they have to be present.
4. Repeat per person, up to 5. Removing a number from this list frees its slot.
5. Collect every number in **E.164 without the `+`**, comma-separated, into `ALLOWED_WA_IDS`:

```
ALLOWED_WA_IDS=447700900123,447700900456
```

### Number format — the country code is included, the `+` is not

Two formats are in play and it's easy to mix them up: the **dashboard** takes the `+`/country
picker, `.env` takes **bare digits**. Bare digits still include the country code — this is the
exact string Meta puts in the webhook's `from` field, and the allowlist compares against it
literally.

The trap is the national **trunk `0`**: you drop it.

| Written locally | `ALLOWED_WA_IDS` | Not |
|---|---|---|
| UK `07700 900123` | `447700900123` | ~~`4407700900123`~~, ~~`07700900123`~~ |
| India `098765 43210` | `919876543210` | ~~`91098765 43210`~~ |
| US `(555) 010-1234` | `15550101234` | ~~`+15550101234`~~ |

No `+`, no spaces, no dashes, no brackets, no leading `0`. Country calling codes never start
with `0`, so a leading zero is always wrong. `wa_preflight.py` (step 7) checks all of this and
names the specific problem, so run it before debugging anything else.

To confirm your own id rather than deriving it: message the test number once with the webhook
running (step 10) — the server logs `from <id>`, which is authoritative.

### The two lists must agree

| List | Where | Effect if a number is missing |
|---|---|---|
| Meta recipient list | dashboard (this step) | Meta refuses the send — error `131030`. Inbound messages from that number never reach you either |
| `ALLOWED_WA_IDS` | `.env` | Message arrives at the webhook and is silently dropped; log shows `sender ... not in ALLOWED_WA_IDS` |

`uv run python scripts/wa_preflight.py --send` (step 7) messages every number in
`ALLOWED_WA_IDS`, which is the fastest way to prove the two lists match.

**Each recipient has an independent 24-hour service window.** The window opens when *that*
person messages the bot. So the bot can reply to whoever just messaged it, but it cannot
spontaneously message a second person who has been quiet for a day — that needs an approved
template (PLAN.md §7, phase 6). Not a problem for a reply-only logging bot; it is a problem the
moment you want proactive nudges to both parents.

## 4. Send the canned test message

Click **Send message** on the API Setup page. You should receive a "Hello World" template on
your phone. If this fails, nothing downstream will work — fix it here first.

## 5. Get a PERMANENT token

The token shown on the API Setup page **expires in 24 hours**. Do not build against it.

1. Go to <https://business.facebook.com/settings/system-users>
   (Business Settings → Users → **System users**).
2. **Add** → name it `assistant-bot` → role **Admin**.
3. **Add assets** → select your app → grant **Full control**.
   Then add your WhatsApp Business Account asset with full control too.
4. **Generate new token** → select your app → expiration **Never**.
5. Scopes: tick **`whatsapp_business_messaging`** and **`whatsapp_business_management`**.
6. Copy the token immediately — it is shown once → `WA_ACCESS_TOKEN`.

## 6. App secret

App dashboard → **Settings → Basic** → **App secret** → Show → `WA_APP_SECRET`.

This is what verifies the `X-Hub-Signature-256` header, so the webhook ignores anyone who
finds your URL.

## 7. Prove the outbound half before touching the webhook

At this point `.env` has everything needed to *send*. Verify that in isolation — a webhook that
never replies is far harder to debug than a bad token.

```bash
uv run python scripts/wa_preflight.py           # checks env, token validity, token lifetime
uv run python scripts/wa_preflight.py --send    # actually messages your phone
```

This resolves `WA_PHONE_NUMBER_ID` against the Graph API (catches pasting the phone *number*
instead of its ID), confirms the token never expires, and checks both required scopes.

Note on `--send`: a free-form text only delivers inside an open 24-hour service window. Right
after step 4's template you have one. If you get error `131047`, message the test number from
your phone and retry — that's correct behaviour, not a broken token.

Once this passes, every remaining failure is an inbound problem.

## 8. Start the tunnel

### Why a tunnel at all

Meta has to `POST` to your webhook, so it needs a **public HTTPS URL**. In development your
server is `localhost:8000` — behind NAT, no public IP, no domain, no TLS certificate. Meta
cannot reach it.

`cloudflared` (Cloudflare's tunnel client) inverts the direction. It opens an *outbound*
connection from your machine to Cloudflare's edge, and Cloudflare gives you a public hostname
that forwards back down it:

```
Meta ──► https://random-words-1234.trycloudflare.com/webhook/whatsapp
           └─► Cloudflare edge ──► (outbound tunnel) ──► localhost:8000
```

No port forwarding, no router changes, no certificate. Free, and this "quick tunnel" mode needs
no Cloudflare account.

Two consequences worth internalising:

- That URL is **genuinely public** while the tunnel is up. Anyone who finds it can POST to it.
  That is why signature verification and the allowlist are in the spike from the beginning
  rather than added later.
- Development only. Production is Fly.io with a stable hostname (PLAN.md §4), so the tunnel
  goes away at Phase 4.

### Run it

```bash
brew install cloudflared            # one-time (already installed on this machine)
uv run uvicorn scripts.spike_whatsapp:app --port 8000 --reload
cloudflared tunnel --url http://localhost:8000        # in a second terminal
```

`cloudflared` prints a URL like `https://random-words-1234.trycloudflare.com`.

Prove the tunnel works **before** involving Meta — otherwise a failed handshake in step 9 has
two possible causes instead of one:

```bash
curl https://random-words-1234.trycloudflare.com/health
```

Expect JSON showing which env vars are set. If this hangs or 502s, the tunnel or uvicorn is
down and nothing in step 9 can succeed.

Note: the quick-tunnel hostname is random and **changes every restart**, so you must re-point
the Meta webhook each time. For a stable hostname, set up a named tunnel
(`cloudflared tunnel create`) — worth doing if Phase 0 stretches over multiple sessions.

## 9. Configure the webhook

Keep the tunnel and uvicorn from step 8 **running** — saving the webhook makes Meta immediately
GET your callback URL, and it fails if nothing answers.

### Finding the Configuration panel

There are **two** locations and which one you get depends on how the app was created. If the
first path isn't in your sidebar, use the second — it is the same panel, just relocated:

| App created as | Path |
|---|---|
| Use case **Other** → Business (step 1) | **WhatsApp → Configuration** |
| Use case **"Connect with customers through WhatsApp"** | **Use cases → Customize → Configuration** |

Meta pushes the second flow by default, so landing there is normal, not a mistake. If neither
appears at all, WhatsApp was never added as a product — go back to step 2.

### Setting it

1. Pick any random string as `WA_VERIFY_TOKEN` in `.env` and restart uvicorn.
2. Open the Configuration panel (above) → Webhook → **Edit**.
3. Callback URL: `https://<tunnel-host>/webhook/whatsapp`
4. Verify token: the same string.
5. **Verify and save.** The server log should print `webhook verification succeeded`.
   Failure here is almost always a token mismatch or uvicorn not running.
6. Under **Webhook fields**, click **Manage** → subscribe to **`messages`**.
   Easy to miss, and without it no inbound messages arrive.

### If the dashboard is uncooperative

The same thing can be done over the Graph API, which skips the UI entirely:

```bash
uv run python scripts/wa_webhook.py --show                              # current subscription
uv run python scripts/wa_webhook.py --set https://<tunnel-host>/webhook/whatsapp
```

This sets the callback URL, the verify token, and the `messages` field subscription in one
call — i.e. steps 2–6 above. It needs `WA_APP_ID` in `.env` (App Dashboard → **Settings →
Basic**, next to the app secret). Because the tunnel hostname changes on every `cloudflared`
restart, `--set` is also the fastest way to re-point the webhook on later sessions.

## 10. Test

**Which number to message:** the **test number** Meta assigned to your app — the human-readable
one in the **From** dropdown on the API Setup page (e.g. `+1 555 012 3456`). Not the
`Phone number ID`; that is an internal identifier and cannot be dialled or messaged.

You don't need to save it as a contact. Step 4's "Hello World" template already created that
chat thread on your phone — open it and reply there.

**Send from a registered phone** (step 3) and expect `echo: <your text>` back in the same chat.

Watch the uvicorn terminal rather than your phone; it localises any failure immediately:

```
inbound: {'object': 'whatsapp_business_account', 'entry': [...]}
from 447700900123: '11am 90ml breast milk'
sent -> 447700900123
```

| Terminal | Meaning |
|---|---|
| nothing at all | Not subscribed to `messages`. Check `wa_webhook.py --show` |
| `inbound:` then `rejected: sender ...` | Your id doesn't match `ALLOWED_WA_IDS` — the log prints the real one, copy it verbatim |
| `from ...` then `send failed` | Inbound fine, outbound broken. The logged error body names the cause |
| all three lines, nothing on the phone | Service window, but you just messaged it, so investigate the error body instead |

The `from <id>` in that log is **authoritative** — it is exactly the string the allowlist
compares against. If it disagrees with your `ALLOWED_WA_IDS` entry, trust the log.

Repeat from **every** registered phone. A number absent from `ALLOWED_WA_IDS` and a number
absent from Meta's recipient list both look like silence to the sender but need different fixes.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Verification fails | `WA_VERIFY_TOKEN` mismatch, or tunnel/uvicorn down |
| Verified but no inbound logs | Either the `messages` field isn't subscribed (step 9.6), **or your app isn't subscribed to the WABA** — see below |
| Inbound logs but no reply | Bad `WA_ACCESS_TOKEN` (expired temp token?) or wrong `WA_PHONE_NUMBER_ID` |
| `rejected: sender ... not in ALLOWED_WA_IDS` | Wrong format — E.164 with no `+`, no spaces |
| `(#131030) recipient not in allowed list` | Recipient not registered in step 3 |
| Reply after 24h of silence fails | Service window closed — message the bot first. Expected behaviour, not a bug |

### The silent one: app not subscribed to the WABA

This cost hours during Spike B, so it is worth understanding rather than just fixing.

**Three different relationships get confused with each other**, and the dashboard only shows the
first two:

| Relationship | Where you can see it |
|---|---|
| Your business portfolio **owns** the WABA | Business Settings → Accounts → WhatsApp Accounts |
| Your system user has the WABA **as an assigned asset** | Business Settings → Users → System users |
| The WABA **emits webhooks to your app** | **API only — no dashboard screen shows this** |

Ownership does not imply event routing. A WABA you fully own, that appears correctly under
WhatsApp Accounts, can be emitting webhooks to *zero* of your apps. A fresh test WABA lists only
Meta's internal `WA DevX Webhook Events 1P App`.

Symptoms are indistinguishable from a working setup: the verification handshake succeeds,
`active: true`, `messages` is subscribed, the callback URL is correct — and no message ever
arrives. Nothing warns you.

```bash
uv run python scripts/wa_webhook.py --show            # names your app if it is subscribed
uv run python scripts/wa_webhook.py --subscribe-waba  # fix
```

Why it is usually invisible: completing the webhook setup **in the dashboard** does both
subscriptions in one action, so you never learn they are separate. Doing it via
`/{app-id}/subscriptions` alone — the API workaround in step 9 — does only half.
