# Deployment — VPS Runbook

> ### ⚠️ Before you expose this
>
> **GET routes are unauthenticated by default.** `/applications` and `/followups`
> reveal where you applied, what was rejected, and where you are interviewing.
>
> That is safe on the default `127.0.0.1` bind. It is **not** safe the moment the API
> is reachable from anywhere else — which includes setting `HOST=0.0.0.0` and the
> split-deploy path below, where a hosted dashboard needs a publicly reachable API.
>
> Either put the API behind a proxy that authenticates, a VPN, or an IP allow-list —
> or turn on read authentication:
>
> ```bash
> JOBAGENT_REQUIRE_AUTH_READS=true      # on the API
> JOBAGENT_API_TOKEN=$(python scripts/api_token.py)   # on the dashboard
> ```
>
> See [SECURITY.md](../SECURITY.md).


Runs the agent autonomously on a Linux VPS: the **Telegram bot** as a persistent
service, an **ingest+match** pass every 4 hours, and a **daily digest** push.

## Why systemd for the pipeline
The ingest→match→digest pipeline is deterministic plumbing, so it runs on **systemd
timers** — universally reliable, observable via `journalctl`, restart-on-failure.
(CV tailoring, cover letters, and fit checks are plain LLM calls inside the service
layer — see `docs/ARCHITECTURE.md`; there is no separate agent process to run.)

## Process model
| Unit | Type | Schedule | What |
|---|---|---|---|
| `jobagent-bot.service` | long-running | always (restart on fail) | Telegram bot — `/jobs`, `/status` |
| `jobagent-ingest.timer` → `jobagent-ingest.service` | oneshot | every 4h (+jitter) | ingest + match, no push |
| `jobagent-pipeline.timer` → `jobagent-pipeline.service` | oneshot | daily 07:00 local | ingest + match + **send digest** |

Cadence is set in `.env` — `INGEST_EVERY_HOURS` (default 4), `DIGEST_AT` (default `07:00`,
server-local), `INGEST_JITTER_SEC` (default 300). `install_services.sh` renders the timers
from those, so the "every 4h" / "daily 07:00" above are just the defaults.

## Prerequisites
- A Linux VPS (Ubuntu/Debian) with sudo. No GPU needed (LLM is via OpenRouter).
- This repo cloned on the box (e.g. `~/PersonalAgent`).

## Provider quickstart — Oracle Cloud (Always Free)
$0 forever. No inbound ports needed (the bot polls; it doesn't receive webhooks) —
only SSH (open by default), so skip all security-list/firewall edits.

1. **SSH key (on your Mac, once):**
   `ssh-keygen -t ed25519 -C jobagent` → copy `~/.ssh/id_ed25519.pub`.
2. **Sign up** at cloud.oracle.com (card used only for identity; Always Free never
   charges). Your *home region* is permanent — pick one near you.
3. **Create instance:** Console → Compute → Instances → *Create instance*.
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: *Edit* → **Ampere (VM.Standard.A1.Flex)**, set **1 OCPU / 6 GB** (Always
     Free). If you see "out of host capacity", switch to **VM.Standard.E2.1.Micro**
     (AMD) or try another Availability Domain/region.
   - SSH keys: *Paste public keys* → paste your `id_ed25519.pub`.
   - Keep "Create new VCN" + assign public IPv4. Create.
4. **Connect:** copy the instance's Public IP, then `ssh ubuntu@<PUBLIC_IP>`.

ARM note: the stack is pure-Python (httpx/pydantic/telethon/ptb all have aarch64
wheels), so Ampere works out of the box.

## Cloning a PRIVATE repo on the VPS
The repo holds your CV, so it's likely private. Easiest auth:
```bash
sudo apt update && sudo apt install -y gh git
gh auth login          # choose GitHub.com → HTTPS → paste a token / browser code
git clone https://github.com/<you>/<repo>.git ~/PersonalAgent
```
(Or add a read-only deploy key: `ssh-keygen -t ed25519 -f ~/.ssh/deploy` →
add the `.pub` under repo *Settings → Deploy keys*, then clone the `git@` URL.)

## Steps

```bash
# 1. Bootstrap: installs uv, venv, deps, initializes the store
cd ~/PersonalAgent
bash scripts/vps_setup.sh

# 2. Configure secrets
cp .env.example .env && nano .env
#    Required: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#              TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, TELEGRAM_CHANNELS

# 3. One-time Telegram user login (for channel reading) — interactive
.venv/bin/python scripts/telegram_login.py

# 4. Smoke test the pipeline WITHOUT pushing to Telegram
.venv/bin/python scripts/pipeline.py --no-send

# 5. (Recommended) set the timezone so 07:00 means your local time
sudo timedatectl set-timezone <Region>/<City>   # e.g. Europe/Berlin

# 6. Install + start all services
sudo bash scripts/install_services.sh
```

## Verify
```bash
systemctl status jobagent-bot.service        # bot should be "active (running)"
systemctl list-timers 'jobagent-*'           # next run times
journalctl -u jobagent-bot -f                # live bot logs
journalctl -u jobagent-pipeline --since today
sudo systemctl start jobagent-pipeline.service   # trigger a digest now
```
Then DM your bot `/status` and `/jobs`.

## Operations
- **Change schedule:** edit `INGEST_EVERY_HOURS` / `DIGEST_AT` / `INGEST_JITTER_SEC` in `.env`, then re-run `sudo bash scripts/install_services.sh`.
- **Update code:** `git pull` → `uv pip install -e ".[telegram,llm]"` → `sudo systemctl restart jobagent-bot`.
- **Logs:** `journalctl -u jobagent-ingest --since '1 day ago'`.
- **Stop everything:** `sudo systemctl disable --now jobagent-bot jobagent-pipeline.timer jobagent-ingest.timer`.

## Security
- `.env` and `*.session` never leave the VPS and are gitignored.
- The bot is owner-locked to `TELEGRAM_CHAT_ID` (fails closed).
- Keep Telegram fetch rates conservative — the default 4h cadence + jitter is deliberate; if you lower `INGEST_EVERY_HOURS`, keep it polite to the sources.
