# Deploy on an Oracle Cloud "Always Free" VM

Zero cost, always-on, persistent journal, warm Huckleberry session — the current design runs
unchanged. You provision a small free VM; the Compose stack (app + Caddy for HTTPS + DuckDNS
for a stable hostname) does the rest.

Total time: ~30–45 min, most of it Oracle's console. The two things that trip everyone up are
called out with ⚠️.

---

## 0. What you'll end up with

```
WhatsApp → Meta → https://<you>.duckdns.org/webhook/whatsapp
                    │  (Caddy: auto HTTPS on :443)
                    ▼
                  app:8000  (FastAPI bot)  ──► Huckleberry
                    │
                  /data/journal.db  (persistent volume)
```

## 1. Create the VM

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (a card is required for identity but
   **Always Free resources are never charged** — you can also set the account to stay on Always
   Free only).
2. **Compute → Instances → Create instance.**
   - **Shape:** `VM.Standard.A1.Flex` (Ampere ARM) — pick **1 OCPU / 6 GB RAM** (well within the
     Always Free 4 OCPU / 24 GB Ampere allowance). Our image is multi-arch, so ARM is fine.
     - ⚠️ If you get *"Out of host capacity"*, try a different **Availability Domain** in the
       dropdown, or retry later — free ARM capacity is region-dependent. Fallback: the x86
       `VM.Standard.E2.1.Micro` (1 GB RAM) also works, just tighter.
   - **Image:** Canonical **Ubuntu 22.04** (or 24.04).
   - **SSH keys:** upload your public key (or let Oracle generate one and download it).
3. Note the instance's **public IP** once it's running.

## 2. Open ports 80 + 443 (two layers — this is the #1 gotcha)

⚠️ Oracle blocks inbound traffic in **two** places. You must open both, or Let's Encrypt and the
webhook will silently fail.

**Layer 1 — VCN security list (cloud firewall):**
- Instance page → **Virtual Cloud Network** → **Security Lists** → the default list →
  **Add Ingress Rules**. Add two rules:
  - Source `0.0.0.0/0`, IP Protocol TCP, Destination port **80**
  - Source `0.0.0.0/0`, IP Protocol TCP, Destination port **443**

**Layer 2 — the VM's own iptables** (Oracle Ubuntu images ship a restrictive ruleset that only
allows port 22). SSH in first:

```bash
ssh ubuntu@<PUBLIC_IP>
```

Then open 80/443 *before* the existing REJECT rule and persist it:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Sanity-check that both ACCEPTs sit **above** the `REJECT ... icmp-host-prohibited` line:

```bash
sudo iptables -L INPUT --line-numbers -n | grep -nE 'dpt:(80|443)|REJECT'
```

If an ACCEPT ended up below the REJECT, delete it (`sudo iptables -D INPUT <line>`) and re-insert
at a lower line number, then `sudo netfilter-persistent save` again.

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker            # or log out and back in, so `docker` works without sudo
docker compose version   # confirms the Compose plugin is present
```

## 4. Get a free DuckDNS hostname

1. Go to [duckdns.org](https://www.duckdns.org), sign in (GitHub/Google), and create a subdomain,
   e.g. `myhuck` → gives you `myhuck.duckdns.org`.
2. Copy your **token** from the top of the page.

(You don't need to set the IP by hand — the `duckdns` container keeps it pointed at the VM.)

## 5. Get the code and secrets onto the VM

There's no remote git repo, so copy the project folder straight from your Mac with `rsync` over
SSH. This also carries your existing local `.env` (real secrets) along, so you don't re-enter them.

Run this **on your Mac** (a local terminal, not the SSH session). Replace the key file and IP:

```bash
rsync -av --progress \
  -e "ssh -i ~/Downloads/<your-private-key-file>" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '*.db' --exclude '.pytest_cache' \
  /Users/mjs/Documents/Products/WhatsApp-Assistant/ \
  ubuntu@<PUBLIC_IP>:~/whatsapp-assistant/
```

The trailing slashes matter (copy the folder *contents* into the destination). The excludes skip
the virtualenv, local journal DB, and caches. `.env` is included — which is what we want.

Then, **on the VM**, add the deploy keys to the copied `.env`:

```bash
cd ~/whatsapp-assistant
nano .env
```

Fill in the deploy keys at the bottom (the other secrets came over with the copy):

```
SITE_ADDRESS=myhuck.duckdns.org
DUCKDNS_SUBDOMAIN=myhuck
DUCKDNS_TOKEN=<your duckdns token>
```

Leave `DB_PATH=./journal.db` as-is — Compose overrides it to the persistent volume automatically.

⚠️ Never commit `.env` (it's gitignored). It holds your Huckleberry password and family phone
numbers.

## 6. Launch

```bash
docker compose up -d --build
```

First build takes a few minutes (installing Python 3.14 + deps). Then verify, in order:

```bash
# a) app is healthy inside the network
# (the slim image has no `curl`, so use Python, which is in the container)
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"

# b) DuckDNS resolves to this VM
dig +short myhuck.duckdns.org        # should print the VM's public IP

# c) Caddy issued a cert and is serving HTTPS (may take ~30s on first run)
curl -s https://myhuck.duckdns.org/health

# watch logs if anything's off
docker compose logs -f caddy         # cert issuance
docker compose logs -f app           # the bot
```

`GET /health` should return JSON with `"llm":"nvidia"` and `"huckleberry_configured":true`.

## 7. Point the Meta webhook at the VM

The webhook URL is now stable: `https://myhuck.duckdns.org/webhook/whatsapp`. Set it (from the VM,
which has the code + creds):

```bash
docker compose exec app python scripts/wa_webhook.py --set https://myhuck.duckdns.org/webhook/whatsapp
docker compose exec app python scripts/wa_webhook.py --show
```

`--show` should confirm the callback URL and that the `messages` field is subscribed. (You can also
set it from your laptop, or in the Meta dashboard — same URL.)

## 8. Test

Message the test number from a registered phone — e.g. `11am 90ml breast milk`, `poo nappy`,
`down now`. Watch it land:

```bash
docker compose logs -f app
```

Then confirm the entries appear in the Huckleberry app.

---

## 9. Updating the app after you change code

There's **no git repo** in this setup — you edit files on your Mac, then push them to the VM.
Two ways, pick one:

### Option A — one command (recommended)

A helper script does both steps for you. One-time setup: open [push.sh](push.sh) and fill in your
key path and VM address at the top (or export `ORACLE_KEY` and `ORACLE_HOST`). Then, any time you
change code:

```bash
# On your Mac, from the project folder:
./deploy/push.sh
```

It copies the code up (skipping the journal DB and caches), rebuilds the container on the VM, and
prints the last 20 log lines so you can confirm it came back up.

### Option B — the two steps by hand

```bash
# 1. On your Mac — re-copy the folder:
rsync -av --progress \
  -e "ssh -i ~/Downloads/<your-private-key-file>" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '*.db' --exclude '.pytest_cache' \
  /Users/mjs/Documents/Products/WhatsApp-Assistant/ \
  ubuntu@<PUBLIC_IP>:~/whatsapp-assistant/

# 2. SSH in and rebuild:
ssh -i ~/Downloads/<your-private-key-file> ubuntu@<PUBLIC_IP>
cd ~/whatsapp-assistant && docker compose up -d --build
```

**What's safe about this:** the copy skips `*.db`, so the server's journal (feeds, sleep records,
dedupe state) is never overwritten by your laptop. Your `.env` **does** get overwritten by the
Mac's copy — that's why the deploy keys (`SITE_ADDRESS`, `DUCKDNS_*`) now live in your local `.env`
too, so they survive every push.

> Prefer a real git workflow (`git pull` on the VM instead of rsync)? That needs a private GitHub
> repo and a deploy key on the VM. Ask and I'll set it up — but rsync is simpler for a personal
> project and needs no third party.

---

## Day-to-day ops

⚠️ **Where each command runs:** the `rsync` (and `./deploy/push.sh`) run **on your Mac**;
everything else (`docker compose ...`) runs **on the Oracle VM, inside the SSH session**. Your
laptop only pushes code up and opens the SSH door.

**To update the app after changing code, see §9 above** (`./deploy/push.sh`, or rsync + rebuild).
The commands below are the other routine operations — all run **on the VM**:

```bash
# Logs
docker compose logs -f app

# Restart / stop
docker compose restart app
docker compose down                 # stops everything (journal volume persists)

# Back up the journal (feed/sleep records + who logged what)
docker compose cp app:/data/journal.db ./journal-backup-$(date +%F).db
```

- **Autostart on reboot** is already handled by `restart: unless-stopped` + Docker's own systemd
  unit — the stack comes back after a VM reboot with no action.
- **The journal survives** redeploys and restarts (named volume `journal`), so dedupe and the
  write-ahead log persist — a Meta retry after a restart won't double-log a feed.
- **Security:** only 22/80/443 are open. Keep the VM patched (`sudo apt update && sudo apt upgrade`).

## Alternative HTTPS: Cloudflare Tunnel (only if you own a domain)

If you already have a domain on Cloudflare, a Tunnel avoids opening ports 80/443 entirely
(outbound-only, so you can skip §2's iptables step). It needs the domain on Cloudflare's
nameservers, so it isn't strictly zero-cost unless you already have one — hence DuckDNS + Caddy is
the default here. Ask and I'll write that variant.
