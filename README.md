# Forager 🐻

**Find the jobs you can actually land.**

[![CI](https://github.com/temesgen5335/forager/actions/workflows/tests.yml/badge.svg)](https://github.com/temesgen5335/forager/actions/workflows/tests.yml)
&nbsp;·&nbsp; 821 offline tests &nbsp;·&nbsp; self-hosted &nbsp;·&nbsp; runs with zero credentials

> **Baer** is a bear that never sleeps: it *senses* every board, *hunts* the roles you can
> actually take, *scavenges* the ones others miss, and *stashes* them in your **den** until
> you say go.

Forager is a self-hosted job-hunting agent you run on your own machine. **Baer** — the
assistant inside it — watches Telegram channels and job boards, scores every posting
against **your** CV and preferences, drafts tailored CVs / cover letters / emails, and
fills ATS forms — but **applies only when you approve**. Remote, hybrid, or onsite, it
surfaces the roles you're *truly eligible for*, not a wall of "remote (US only)" you can't
take. You drive it from a Telegram bot, a web dashboard, or the CLI.

**Private by design.** No identity, search profile, or company list is ever committed —
the repo ships templates, and everything personal lives in gitignored config you own. Your
data stays in your den; secrets are encrypted at rest; nothing phones home.

---

## Quickstart — a demo in one command (keyless, ~5 minutes)

```bash
git clone https://github.com/temesgen5335/forager && cd forager
make install       # backend venv + dashboard deps
make quickstart    # writes a ready .env + seeds a demo den, prints your dashboard password
make run           # dashboard on http://127.0.0.1:1234, API on :8077
```

Open the dashboard and you're looking at **ranked demo matches immediately** — no keys, no
sign-ups. When you're ready to make it yours (still no API key required):

```bash
make onboard       # guided setup: profile, sources, LLM (free or paid), email, Telegram
make pipeline      # a real forage (ingest → match) against the public boards
```

**Zero credentials, really.** Six of the seven sources are public APIs, and matching falls
back to transparent heuristics with no LLM key — so you get real, ranked jobs before you
sign up for anything. Add a (free) LLM key later and Baer also drafts your CV and cover
letters.

---

## What Forager does

- **Senses** seven sources — Telegram, RemoteOK, Remotive, Himalayas, Greenhouse, Lever,
  Ashby — normalizes and de-duplicates them, and stashes matches in your **den** (a local
  SQLite store).
- **Hunts** the roles you can *actually take* — preference-weighted scoring plus a
  configurable geographic filter, so `remote_scope="global"` keeps genuinely worldwide-remote
  roles and demotes the ones locked to a region you're not in. On one live 17k-posting den,
  turning it on cut the catch from **311 to the handful of genuinely reachable
  roles** — the rest were remote-US / remote-UK / onsite you couldn't take.
- **Applies** only when you say go — Baer drafts a tailored CV + cover letter + email, shows
  you *exactly what will be sent*, and submits only on your explicit approval. It never
  fabricates experience, and there is deliberately **no auto-apply button**.

## Why Forager

- **It finds jobs you can land, not just "remote."** The geographic filter is the wedge —
  most tools can't tell a truly global role from a US-only one wearing a "remote" label.
- **Human-in-the-loop, never spam.** Baer hands you the send button; you click it. Quality
  over a thousand auto-fired applications employers now filter out.
- **Never fabricates.** Tailoring reframes real experience from your CV — nothing invented.
- **Private + self-hosted.** Your den, your machine; credentials encrypted at rest; the
  assistant can read your data but has **no tool that can send, submit, or approve** — a
  structural property, not a promise.
- **Serious engineering under the hood.** 821 offline tests (zero network), CI on every
  push, a multi-provider LLM harness with automatic failover, and a run-id **trail** that
  audits every action.

## Baer's dialect

Forager speaks in one consistent vocabulary — it's how the docs, and increasingly the tool,
talk about what's happening:

| Term                | What it is                                                          |
| ------------------- | ------------------------------------------------------------------- |
| **Senses**    | your ingestion sources                                              |
| **Forage**    | one ingest → match pass (run it with`make forage`, an alias of `make pipeline`) |
| **Hunt**      | the matcher — the roles you can actually take                      |
| **Scavenge**  | recovery + de-duplication across scattered sources                  |
| **Den**       | your self-hosted SQLite store — private, local, yours              |
| **Trail**     | the run-id audit trail — what Baer did, and why                    |
| **Stash**     | triage — snooze or set a posting aside for later                   |
| **The catch** | a strong match, or a landed application                             |

*(The dialect runs through the tooling now — logs, CLI output, and the code itself speak
this vocabulary. `make pipeline` still works; `make forage` is the same command by its own
name.)*

## Ask Baer

```bash
make ask Q="is the pipeline healthy?"
make ask Q="how much of my catch am I ignoring?"
make doctor                                   # why is it using that model? (offline)
```

Also at `/assistant` in the dashboard and `/ask <question>` in Telegram. Baer reads your
forages, queue, applications and settings — but it **cannot send, submit or approve
anything**; when something needs sending, it hands you a link. Even on an LLM too weak to
run a tool loop it degrades gracefully: retrieval runs in Python and the model only writes
the answer (measured 100% tool-selection / 100% answer-grounding on the free tier).

---

### Let a coding agent operate it (MCP)

Open the repo in Claude Code (or any MCP client): `.mcp.json` registers the
`personalagent` server, which exposes the governed toolbox — pull jobs, list and sort
matches, fit-check, triage, draft, track applications — with every action confirmed by
you and recorded on the trail. It can never send or approve anything (R2).
`make mcp_check` lists the surface offline. See `AGENTS.md` § 3a.

<details>
<summary><b>Advanced — manual setup, LLM providers, Docker, deploy, and internals</b></summary>

### Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Node 18+ (only for the dashboard)
- Optional: a Telegram bot from [@BotFather](https://t.me/BotFather); at least one LLM key
  (free options below)

### Security note

Every state-changing API route requires a bearer token, so `DASHBOARD_PASSWORD` must be set
for applying, status edits, fit checks, or config changes. Read-only endpoints stay open.
Never expose the API publicly without it — those routes can send email as you. `make quickstart` and `make onboard` generate this password for you.

### Manual setup (what `make onboard` does, by hand)

```bash
uv venv && uv pip install -e ".[telegram,llm,apply]"
.venv/bin/playwright install chromium        # only for Tier-2 ATS form-fill
cp .env.example .env                          # then edit — see keys below
cp config/preferences.example.toml config/preferences.toml   # or edit in Settings → Profile
```

Your profile decides everything the hunt scores against (`target_roles`, `core_skills`,
per-skill `skill_weights`, `remote_scope`, `[sources]`, `[watchlist]`). Edit it in
**Settings → Profile** (writes a gitignored `data/profile.json`) or in the TOML. Your CV
text goes in **Settings → CV & background** (`data/cv_master.md`); the PDF at `cv_path` is
what gets attached to email applications. Layering, lowest priority first:
`config/preferences.example.toml` → your gitignored `preferences.toml` → the legacy
`preferences.local.toml` → `data/profile.json` (the only layer the UI writes).

### LLM options (all OpenAI-compatible except Anthropic)

| Provider                 | Free tier          | Set                     | Notes                                 |
| ------------------------ | ------------------ | ----------------------- | ------------------------------------- |
| Groq                     | ✅ generous        | `GROQ_API_KEY`        | fast; good default primary            |
| Google Gemini            | ✅ (check quota)   | `GEMINI_API_KEY`      | via OpenAI-compat endpoint            |
| OpenRouter               | ✅`:free` models | `OPENROUTER_API_KEY`  | 200+ models incl. a live free fan-out |
| OpenAI                   | ❌ paid            | `OPENAI_API_KEY`      |                                       |
| Anthropic                | ❌ paid            | `ANTHROPIC_API_KEY`   |                                       |
| Local/OSS (Ollama, vLLM) | ✅ self-run        | `CUSTOM_LLM_BASE_URL` | any OpenAI-compatible server          |

Set `LLM_PROVIDER` to your primary; the rest become automatic failover. No key at all → the
hunt runs heuristic-only (apply-drafting is the only thing that needs a model).

### Docker

```bash
cp .env.example .env                          # or: make setup
make docker_up                                # API + dashboard; data/ is a mounted volume
docker compose --profile bot up -d            # add the Telegram bot
docker compose run --rm pipeline              # one forage
make docker_down
```

Host ports bind to `127.0.0.1` only, because GET routes are unauthenticated — read
[SECURITY.md](SECURITY.md) before changing that.

### Deploy

- **Free daily digest (no server):** GitHub Actions — [docs/DEPLOYMENT_ALTERNATIVES.md](docs/DEPLOYMENT_ALTERNATIVES.md).
- **Full autonomous (bot + scheduled forages):** VPS — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) (Oracle free-tier quickstart included).

### Running & commands

```bash
make check       # preflight: .env, required vars, free ports, store presence
make run_bot     # the Telegram bot (separate long-lived process) — then DM it /menu
make test        # the offline test suite
make run API_PORT=9000 DASH_PORT=4322         # override ports
```

Deploying the dashboard away from the API: set `PUBLIC_JOBAGENT_API_URL` (browser-side) and
`JOBAGENT_API_URL` (SSR), and add the dashboard origin to `JOBAGENT_CORS_ORIGINS`.

```bash
.venv/bin/python scripts/match.py 12            # rescore + print top matches
.venv/bin/python scripts/apply.py prepare 3     # draft a Tier-1 (email) application
.venv/bin/python scripts/apply_ats.py preview 3 # Tier-2 ATS fill + screenshot (no submit)
```

### Architecture

```
Telegram / RemoteOK / Remotive / Himalayas / Greenhouse / Lever / Ashby
        └─▶ senses (adapters) ─▶ den (SQLite) ─▶ hunt (heuristic + LLM)
                                     │                    │
                                     ▼                    ▼
                        Telegram bot · dashboard · Baer  ◀─  ranked catch / apply
```

A **FastAPI orchestrator** sits between the interfaces and the data; the dashboard calls it
over REST and the bot calls the same service layer in-process. Multi-provider LLM with
automatic failover. Full write-up: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Reusing the agent harness

`src/agentkit/` is domain-agnostic — multi-provider LLM with capability routing, governed
tools, fenced retrieval, a fail-closed audit trail — and imports nothing from this
application. Full guide: [src/agentkit/README.md](src/agentkit/README.md).

</details>

## Hard rules

See [.claude/rules.md](.claude/rules.md): never fabricate CVs · never submit without
per-job approval · prefer APIs over scraping · secrets only in `.env` / the encrypted store
· don't fight CAPTCHA.

## Versions & roadmap

What changed: [CHANGELOG.md](CHANGELOG.md) · what ships next: [docs/ROADMAP.md](docs/ROADMAP.md)
· how versions are decided: [docs/VERSIONING.md](docs/VERSIONING.md).

## Contributing / onboarding an agent

Start at [CLAUDE.md](CLAUDE.md) (or [AGENTS.md](AGENTS.md) for other tools), which points at
`.claude/`: `context.md` (problem, vision, current state), `rules.md` (hard constraints),
`agent.md` (architecture, stack, module map), `memory.md` (why the design is the way it is).
Read those before changing anything.
