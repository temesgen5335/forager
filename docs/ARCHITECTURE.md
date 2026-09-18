# Architecture

## Principle
There is no agent framework in this system, and no LLM-driven control loop. The
pipeline is deterministic plumbing — ingest → dedup → score → surface → assist — and
the LLM is called only where judgment actually helps: reranking matches, assessing
fit, and drafting application text. Scheduling is **systemd timers** (or GitHub
Actions); the **FastAPI orchestrator** is the single backend; **SQLite** is the store.

> **Historical note.** Early plans (`docs/PLAN.md`) framed *Hermes Agent* as an
> orchestrating "brain" with MCP tool servers. None of that was ever implemented, and
> the placeholders were removed: every role it would have filled is covered more
> simply by systemd timers, `MultiLLM` provider failover, and `preferences.toml`.
> Ignore Hermes/MCP references in older docs — the code is the authority.

## Component diagram

```
             SOURCES                    ONE PROCESS PER BOX, ONE HOST (VPS)
  Telegram channels (Telethon) ─┐
  RemoteOK / Remotive           ├──▶ ┌─────────────────────────────────────────┐
  Greenhouse / Lever / Ashby    │    │ pipeline.py  (systemd timer / Actions)  │
  (aggregator: planned)        ─┘    │  run_id ─ ingest → match → digest ──────┼──▶ Telegram DM
                                     └──────────────────┬──────────────────────┘    (daily digest +
                                                        ▼                            health banner +
      ┌──────────────────┐  in-process  ┌─────────────────────────────────┐          follow-up nudges)
      │  TELEGRAM BOT    │─────────────▶│         SERVICE LAYER           │
      │  (Bot API,       │              │ ingestion · matching · fit ·    │
      │  owner-gated)    │              │ apply (Tier 1 email / Tier 2    │
      └──────────────────┘              │ ATS via Playwright) · llm ·     │
                                        │ digest · secrets                │
      ┌──────────────────┐    REST      └────────────────┬────────────────┘
      │  ASTRO DASHBOARD │───────────▶ ┌─────────────────┴────────────────┐
      │  (SSR, reads     │             │      FASTAPI  ORCHESTRATOR       │
      │  open; writes    │             │  :8077 — sole backend; bearer    │
      │  carry bearer    │             │  auth on every non-GET route     │
      │  token)          │             └────────────────┬─────────────────┘
      └──────────────────┘                              ▼
                                        ┌──────────────────────────────────┐
                                        │        SQLITE STORE (SSoT)       │
                                        │ jobs · matches · applications ·  │
                                        │ cv_variants · events (trail,     │
                                        │ health)                          │
                                        └──────────────────────────────────┘
```

The bot calls the service layer **in-process** (no HTTP hop); the dashboard calls the
REST API. Both read and write the same store, and every write path funnels through
the same service functions.

## The agent harness

Added Aug 2026, in two packages whose separation is the whole point:

```
   CLI  ·  /assistant page  ·  Telegram /ask        three renderers, one mechanism
                     │
                     ▼
   ┌──────────────────────────────────────┐   supplies tools, knowledge,
   │  jobagent/assistant  (DOMAIN)        │   prompts, policy — via a manifest
   │  15 in-process tools · CONFIG_       │
   │  WRITABLE · FTS5 over postings       │
   └───────────────────┬──────────────────┘
                       ▼
   ┌──────────────────────────────────────┐   stdlib + pydantic only.
   │  agentkit  (DOMAIN-AGNOSTIC)         │   NEVER imports jobagent (R30)
   │  capability routing · 9 strategies · │
   │  governed toolbox · fail-closed audit│
   └──────────────────────────────────────┘
```

**Route before you run.** `resolve_card()` answers what a model can do — a measured
entry, else a family pattern, else the parameter count in its name, else UNKNOWN
(treated as WEAK, never assumed capable). `plans_for()` filters by that and returns a
ranked queue which *is* the failover queue, so a fallback can never land on a model
that would silently do the job badly. The configured `LLM_PROVIDER` no longer decides
admission — only ties.

**Degrade by changing how, not what.** Nine strategies share one signature. The
load-bearing one moves planning into Python and leaves the model only the write-up,
so a model that emits perfect tool calls but cannot use a tool *result* still answers
correctly. That distinction is measured, not assumed: llama-3.1-8b scores 5/5 on
emitting and selecting, 0/5 on using the result.

**Safety is structural, not procedural.** There is no tool that can send, submit or
approve — those names are in an exclusion set and registering one raises at wiring
time. Frozen configuration is the *computed complement* of a short allow-list, so a
setting added next year is frozen the day it is added. The component that decides
whether an action may run is given no access to the transcript or to retrieved text,
so a prompt injection can make the model *ask* for something but cannot make the
gatekeeper agree. Confirmations are server-side and bound to the exact arguments.

Assistant sessions ride the existing trail (`kind_detail="agent_session"`), so
they are auditable with no new storage — and are filtered out of `list_runs()` by
default, because a session has no ingest counts and would render as a blank pass.

**The MCP server (`src/jobagent/mcp/`) is the fourth renderer** — CLI, dashboard,
Telegram, and now a coding agent over stdio (Claude Code, Codex, any MCP client), all
sharing the one governed toolbox and none of them a second access path. One `Operator`
owns a Store, a `GuardedToolBox` and an `Auditor` on a single dedicated thread
(`ThreadPoolExecutor(max_workers=1)`), so the SDK dispatching sync tools onto worker
threads can never race the gatekeeper or the audit sink (R15) — `pull_jobs` is the
deliberate exception, running a pass on its own thread with its own Store, coordinated
by the same advisory lock the API and scripts use, so a long ingest never blocks the
client's tool-call timeout. Confirmations are SDK **resolvers**: each ACT/ADMIN tool
carries a hidden `Elicit(card, Approve)` parameter that renders the same
`render_card()` a human sees on the web or in Telegram and is bound to `sha256(args)`
by the Gatekeeper underneath, so a retried call with changed arguments is refused
rather than silently re-approved (R29). `Surface.AGENT` is deliberately **not** in
`admin_surfaces`: an MCP client "might decide how to handle the elicitation" rather
than asking a person, so ADMIN (config-changing) tools stay hidden unless the server
is launched with `--admin` — an operator decision made in the client config, not a
runtime toggle the model can flip. `personalagent://` resources are served *through*
`execute()` too, so a resource read is audited exactly like a tool call, never a
side channel around the gatekeeper. The transport is **stdio only for now**: the
protocol-level state question (stateless 2026-07-28 vs. session-based 2025-11-25) is
the client's to make, not ours, but the *application*-level state — session grants,
pending nonces, the one run id — lives on the server process either way, and a
client-carried session handle would travel through the model, which is exactly what
R29 forbids trusting. Streamable HTTP and `--pre-approve` are deferred (see the design
spec §2 and §5.5–5.6 for the full reasoning).

## The two-Telegrams rule (most important design point)
"Telegram" plays two unrelated roles and needs two different mechanisms:

| Role | Mechanism | Credential |
|---|---|---|
| **Read job-posting channels** | Telethon (MTProto, logs in as you) | `api_id` / `api_hash` from my.telegram.org |
| **Talk to your agent** | Bot API | BotFather token |

Never conflate them. The reader lives in `ingestion/adapters/telegram.py`; the bot in
`bot/`. Telethon needs a stable IP and a persisted `.session` file (a user account
logging in from rotating IPs gets flagged); the Bot API is plain outbound HTTPS and
runs anywhere, including CI.

## Source tiers (drives the ingestion design)
| Tier | Sources | Strategy | Risk |
|---|---|---|---|
| Clean API | RemoteOK, Remotive, Greenhouse, Lever, Ashby | direct API client via `get_with_retry` | none |
| MTProto | Telegram channels | Telethon (your account) | low (rate hygiene) |
| **No API, hostile** | Indeed, LinkedIn, Glassdoor | **aggregator** (JSearch/SerpApi) — *planned, toggle exists, no adapter yet* | high — never scrape directly |
| Last resort | any board w/o feed | Playwright | fragile |

## Data flow
1. A systemd timer (or `make pipeline`) starts a pass. The pass mints a **run_id**
   that rides every event it emits.
2. Senses fetch → normalize into `JobPosting` (full payload kept in
   `raw`) → dedup by `sha256(company|title|location)` → upsert. One bad sense logs
   an `error` event and the rest continue.
3. Matching scores **every** job with the preference-weighted heuristic (no API
   cost): tiered role signal (title ≫ tags ≫ body), weighted skill coverage
   (`[profile.skill_weights]`), seniority-mismatch dampening, must-have checks,
   exclusion ceiling. An optional **LLM rerank** rescores the top ~30.
4. The digest DMs the top matches — prefixed by a **health banner** when the run was
   degraded and followed by **follow-up reminders** for applications gone quiet.
5. `/apply` (bot) or the dashboard drafts assets: tailored CV, cover letter, email —
   all grounded on `cv_master.md` (R1: reframe, never invent; the email prompt
   receives the CV precisely so the model cannot mirror the job's requirements back
   as the candidate's background).
6. **HITL gate**: nothing is sent without explicit approval. Tier 1 emails via SMTP;
   Tier 2 fills Greenhouse/Lever/Ashby forms via Playwright, screenshots for preview,
   and pauses again before submit. CAPTCHA → hand back a deep link, never solve.
7. Application statuses move through an enforced lifecycle
   (`ALLOWED_TRANSITIONS`); out-of-process corrections require an explicit, audited
   flag. Follow-ups are **drafts only** — no send path exists.
8. The dashboard reads everything through the API: analytics, funnel, pipeline
   health, the trail, and the same fit checker the bot uses.

## Matching quality
The scorer has a regression net beyond unit tests: a labeled eval set
(`matching/evalset.py`) covering every trap class that has actually bitten —
substring hits, seniority/management mismatches, requirement-mirroring buzzword
bodies, exclusions, tag-flooding. `tests/test_eval_matching.py` pins precision@5 =
1.0, precision/recall@10 ≥ 0.9, separation ≥ 0.42; `scripts/eval_matching.py` prints
the ranked table for tuning. Floors sit at measured reality; the one known miss
(tag-flooding) is documented in the dataset rather than hidden.

## Observability
One **run_id** threads ingest → match → digest. Matching logs a `match` event; each
pass ends with a `run` summary (duration, per-stage counts, digest outcome) that
`GET /runs` lists and `GET /runs/{id}` expands into the pass's full event sequence.
`Store.pipeline_health()` derives staleness, a windowed error count, and per-sense
freshness from the same trail; the dashboard banners staleness and the digest carries
a warning prefix, so a silently dead pipeline is a state the UI cannot render as
healthy.

## Security posture
Every state-changing API route (`/apply/*`, `/ats/*`, `PATCH /applications/*`,
`/ingest`, `/match`, `/fit`, `/followups/*/draft`, `/config`) requires a bearer token
derived from `DASHBOARD_PASSWORD`, and **fails closed** when no password is set.
Read-only routes stay open so the dashboard renders server-side.
`JOBAGENT_CORS_ORIGINS` defaults to localhost. Rationale: these routes can send email
as you, so port reachability must not equal authority. A route-table test fails if a
new write route ships ungated. Secrets live in `.env` or the Fernet-encrypted store
(masked on read); identity lives in gitignored `config/preferences.local.toml`, so
the committed config carries placeholders only.

## Resilience
Sense HTTP goes through `get_with_retry` — bounded exponential backoff with full
jitter, retrying 429/5xx and transport errors while treating other 4xx as permanent,
honoring a capped `Retry-After`. A failing sense logs an `error` event and the run
continues. Recovery is graceful everywhere: LLM providers fail over in an ordered
chain, a missing LLM degrades matching to heuristic-only and fit to the offline
report, and a failed digest send is reported on the trail rather than crashing
the pass.

## Tech stack
Python 3.11+ · pydantic v2 · **FastAPI** (orchestrator, :8077) · SQLite + **FTS5**
(→ Postgres if ever needed) · Telethon + python-telegram-bot · Playwright (Tier 2) ·
capability-aware multi-provider LLM routing (Groq/Gemini/OpenRouter/Qwen/OpenAI/
Anthropic + any OpenAI-compatible endpoint) · **Astro** SSR dashboard · systemd
timers on a VPS · GitHub Actions for the serverless digest.

Retrieval is FTS5 rather than embeddings: it ships with SQLite, so there is no vector
store, no model download and no reindex job — and for "which document mentions this
identifier", which is what operational questions actually are, it beats a small
embedding model outright.

## Diagnosing it
`scripts/llm_doctor.py` prints the chain, every model card and where its capabilities came
from, and for each task shape which plan wins and why every other was rejected. It is
offline unless you pass `--probe`. Reach for it first — the routing deliberately does
not obey `LLM_PROVIDER`, which looks like a bug the first time you see it.
