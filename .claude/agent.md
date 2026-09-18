# Agent Guide — Architecture, Tools & Frameworks

This document explains the design choices, technology stack, and module structure
so any developer or AI agent can navigate the codebase and contribute immediately.

---

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│                     Oracle Cloud VM                        │
│                                                            │
│  ┌─────────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  FastAPI :8077   │  │ Telegram Bot │  │   Telethon   │  │
│  │  (orchestrator)  │  │ (Bot API)    │  │   (reader)   │  │
│  └────────┬────────┘  └──────┬───────┘  └──────────────┘  │
│           │                  │                              │
│           ▼                  ▼                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           Service Layer (Python modules)             │   │
│  │  ingestion · matching · fit · apply · llm · digest   │   │
│  └────────────────────┬────────────────────────────────┘   │
│                       ▼                                    │
│            ┌────────────────────┐                          │
│            │  SQLite Store      │                          │
│            │  (SSoT, on disk)   │                          │
│            └────────────────────┘                          │
│                                                            │
│  systemd timers: ingest (4h) · pipeline+digest (daily)     │
└───────────────────────────────────────────────────────────┘
         ▲                              ▲
         │ REST API                     │ SSR fetch
    ┌────┴────────┐              ┌──────┴──────┐
    │ Vercel      │              │  GitHub      │
    │ Astro SSR   │              │  Actions     │
    │ (dashboard) │              │  (digest CI) │
    └─────────────┘              └─────────────┘
```

### Data Flow

1. **Senses** fetch every job posting → normalize to `JobPosting` → dedup → store.
2. **Matching engine** scores new jobs: heuristic pre-filter (always) + optional LLM rerank.
3. **Telegram bot** pushes ranked digest; user taps to see fit-check, then apply.
4. **Apply flow** generates assets (CV variant, cover letter, email) via LLM. HITL gate.
5. **ATS executor** fills Greenhouse/Lever/Ashby forms via Playwright. Screenshot preview before submit.
6. **Dashboard** reads the same store via the FastAPI API for analytics and configuration.

### Key Design Decisions

| Decision | Rationale |
|---|---|
| SQLite, not Postgres | Single-user self-hosted system. No connection pooling, no ORM, no migrations server. Per-request open/close for thread safety. |
| FastAPI as sole backend | One process to manage. Bot calls service layer in-process (no HTTP hop = faster). Dashboard calls REST API. |
| Multi-LLM failover, free-first | `MultiLLM` chains providers: primary from `LLM_PROVIDER`, then free backups (Groq → Gemini → OpenRouter). Paid providers (OpenAI, Anthropic) only if configured. Falls through on any error. |
| Heuristic + LLM matching | Heuristic runs on every job (zero cost). LLM only reranks the top candidates. Word-boundary regex avoids substring false-positives ("Go" ≠ "going"). |
| Fernet encrypted secret store | Dashboard settings page writes to an encrypted file on disk. `.env` is the base; secret store overlays it. Config changes apply without restart via `reload_settings()`. |
| Auth on writes, not reads | Write routes can send email and submit forms as the user, so they need a bearer token and fail closed without `DASHBOARD_PASSWORD`. Reads stay open because the dashboard renders them server-side with no token to offer. |
| Two API base URLs | `JOBAGENT_API_URL` is what the *server* calls; `PUBLIC_JOBAGENT_API_URL` is what the *browser* calls. A server-side `127.0.0.1` means the visitor's own laptop once it reaches a browser — which is why the split exists. |
| Identity overlay | `preferences.local.toml` (gitignored) overlays `preferences.toml` (committed placeholders) section-wise, so a clone carries a working search profile but nobody's contact details. |
| Telethon + Bot API (two Telegrams) | Telethon (MTProto, user account) for reading channels. Bot API (BotFather token) for the interactive bot. Never conflate them. |
| HITL gate for all submissions | R2. Both email (Tier 1) and ATS (Tier 2) stop and show the user what will be sent. `approved_at` stamped only on explicit action. |
| Profile is data, not code | Identity, background, CV and search preferences persist to a gitignored `data/profile.json` + `data/cv_master.md` overlay, edited through Settings → the profile tabs and `/profile`. The tree carries placeholders only (R22). Loaded fresh per request so edits apply without a restart. |
| Harness split in two packages | `agentkit` is domain-agnostic and may never import `jobagent` (R30); `jobagent/assistant` supplies tools, knowledge, prompts and policy through a manifest. If wiring a second application required editing `agentkit`, the boundary would be decorative — so three tests hold it. |
| Route before running | `plans_for()` filters backends by capability *first* and returns a ranked queue that doubles as the failover queue. An incapable backend never enters it, so failover cannot land on a model that would silently do the job badly. Consequence worth knowing: `LLM_PROVIDER` no longer decides admission, only ties — a chosen primary that cannot do a task is skipped, with a reason. |
| Probe before you need it | Failover is reactive: it finds a dead provider by calling it and waiting. `LLMService.preflight()` calls every backend **concurrently** and reorders by measured latency, so a chain with dead backends in front stops paying their timeouts on every request. Reordering never changes membership — a probe is a snapshot, not a verdict. |
| A trace, not just a breaker | The breaker knows if a backend is open *now*. The `Ledger` answers the question an operator actually asks: which providers work, which do not, and why — per-verdict counts, latencies, last error. Two dead slugs survived weeks because nothing answered that; the answers still arrived, from the next provider, a little slower. |
| Degrade by changing *how*, not *what* | Nine strategies behind one signature. The load-bearing one, `prefetch_single_shot`, moves the planning into Python so a model that cannot use a tool *result* still answers. Measured: the same question answered correctly on llama-3.3-70b via a native loop and on llama-3.1-8b (0/5 on loops) via prefetch. |
| Exclusion over gating | A tool the agent must never have is not registered at all (R26). A gate is a runtime check an attacker defeats once; absence has no code path to attack. Same call this repo already made for follow-up sends (R24). |
| Frozen config is the *complement* | `CONFIG_WRITABLE` names what may change; everything else in `MANAGED_FIELDS` is frozen by construction, so a setting added next year is frozen the day it is added. A frozen *list* would default the other way and fail silently. |
| Policy input carries no text | `SessionContext` holds actor, surface and prior grants — no transcript, no retrieved chunks, no model output (R28). An injection can make the model *request* a config rewrite; it cannot make the gatekeeper approve one, because the gatekeeper cannot read it. |
| Confirmations bound to arguments | The nonce is server-side and tied to `sha256(args)`. On HTTP and Telegram the client sends *only* the nonce and the arguments never leave the server, which makes confirm-then-swap structurally impossible rather than merely detected. |
| Assistant sessions share the run spine | A session closes with a `run` event carrying `kind_detail="agent_session"`, so it lands in the existing trail with no new table — and `list_runs()` filters it out by default, because a session has no ingest counts and would render as a blank pass. |
| The MCP is a renderer, not a path | Every MCP tool and resource goes through `GuardedToolBox.execute()` on one owning thread; confirmations are elicitations answered by a person and bound to `sha256(args)` underneath; ADMIN is hidden on the agent surface by default because the server cannot prove a person answered (the same call Telegram got). |

---

## Technology Stack

### Backend (Python 3.11+)
| Tool | Purpose |
|---|---|
| **pydantic v2** | Domain schemas, settings, validation. `ConfigDict` only (no class Config). |
| **pydantic-settings** | `.env` loading with alias mapping. |
| **FastAPI** | REST API orchestrator. `create_app()` factory for testability. |
| **httpx** | HTTP client for the senses (async-capable, sync used). |
| **Telethon** | MTProto Telegram client for reading job-posting channels. |
| **python-telegram-bot** | Bot API wrapper for the interactive bot. |
| **openai SDK** | OpenAI-compatible backend for Groq/OpenRouter/OpenAI/Gemini. |
| **anthropic SDK** | Anthropic-specific backend (Messages API). |
| **Playwright** | Browser automation for ATS form-fill (Tier 2). Lazy import. |
| **cryptography (Fernet)** | Encrypted secret store for dashboard config. |
| **SQLite** | Single-file relational store. stdlib `sqlite3`, no ORM. |

### Dashboard
| Tool | Purpose |
|---|---|
| **Astro 5** | SSR framework. Renders server-side, fetches the FastAPI API. |
| **@astrojs/node** | Node.js SSR adapter for self-hosting. |
| No JS framework | Pure `.astro` components. No React/Vue/Svelte. Minimal JS for interactive bits (fetch on button click). |

### Infrastructure
| Tool | Purpose |
|---|---|
| **systemd** | Process management: bot service (always-on), ingest timer (4h), pipeline timer (daily). |
| **GitHub Actions** | CI test runner + daily API-sense digest (no Telethon needed). |
| **uv** | Fast Python package manager (used in vps_setup.sh). |

### Testing
| Tool | Purpose |
|---|---|
| **pytest** | Test runner. `asyncio_mode=auto`. |
| **FakeLLM / FakePage / FakeSMTP** | Injectable test doubles. No network, no credentials, no browser. |
| **TestClient (FastAPI)** | Sync HTTP testing of the API without uvicorn. |
| **tmp_path + monkeypatch** | Isolated temp stores and env overrides per test. |

---

## Module Map

```
src/jobagent/
├── core/schemas.py          # JobPosting, Match, Application, CVVariant, Event, Source, enums
├── config.py                # Settings (pydantic-settings), get_settings(), reload_settings()
├── preferences.py           # Profile/Watchlist/Sources; 3-layer merge + writable
│                            #   data/profile.json overlay + data/cv_master.md (save_*)
├── store/db.py              # SQLite Store: upsert, query, analytics, application lifecycle
│                            #   _row_predicates() — ONE filter path shared by
│                            #   get_matches() (list) and purge_jobs() (delete)
├── llm_client.py            # MultiLLM: ordered failover chain, OpenAI-compat + Anthropic backends
├── secrets_store.py         # Fernet-encrypted config store, masked_view()
├── lifecycle.py             # transition(): the one function that moves an application (R23)
├── pipeline.py              # forage(): the one ingest → match → summary seam (API, scripts, agent)
├── ingestion/
│   ├── base.py              # BaseAdapter ABC (source, fetch, enabled)
│   ├── util.py              # strip_html, make_client, split_slugs, get_with_retry (R21)
│   ├── runner.py            # run_ingestion() — resilient per-sense with RunReport
│   ├── registry.py          # build_adapters() — watchlist + env slugs, filtered by Sources
│   └── adapters/            # remoteok, remotive, himalayas, greenhouse, lever, ashby, telegram, jsearch
├── matching/
│   ├── heuristic.py         # heuristic_score() — word-boundary keyword matching
│   ├── llm.py               # llm_score() — LLM rerank of top candidates
│   └── engine.py            # run_matching() — heuristic always, LLM optional
├── fit.py                   # FitReport, heuristic_fit(), llm_fit(), assess_fit() — explainable
├── apply/
│   ├── flow.py              # approve_and_send() (HITL gate)
│   ├── prepare.py           # prepare_application() — drafts only; shares no module with the sender
│   ├── generators.py        # tailor_cv(), write_cover_letter(), draft_email(); review_draft()/revise_draft()
│   ├── verify.py            # ats_report(): pure ATS-parseability check on the CV (always on, read-only)
│   ├── pdf_verify.py        # optional PDF text-layer extractor (vendored, MIT) for ats_report_for_pdf()
│   ├── render.py            # optional Markdown→PDF CV render (fpdf2); APPLY_RENDER_CV_PDF closes the verify loop
│   ├── email_send.py        # SMTP sender with attachment
│   ├── ats/fields.py        # detect_platform(), field_plan(), CAPTCHA_SELECTORS
│   ├── ats/executor.py      # execute() (injectable page), apply_to_job() (Playwright)
│   └── ats_flow.py          # create_ats_application(), run_ats()
├── bot/
│   ├── service.py           # Pure helpers: MatchFilter, ranked_matches, jobs_text, HELP_TEXT
│   ├── app.py               # Telegram bot: /menu, /jobs, /apply, /status, callbacks
│   └── notify.py            # send_message() with chunk_text() (4096-char limit)
├── digest.py                # diversify() (per-company cap), format_matches(),
│                            #   health_banner() — degraded-run warnings on the digest
├── upskill.py               # skill_gaps()/upskill_report(): fit-weighted gap heatmap
│                            #   from Match.gaps; learning_plan() is the one LLM step
├── api/
│   ├── app.py               # FastAPI create_app() factory — all REST endpoints
│   │                        #   every non-GET route carries dependencies=auth (R19)
│   └── assistant_routes.py  # /assistant/ask + two-phase /assistant/confirm/{nonce}
├── assistant/               # THE DOMAIN HALF OF THE AGENT HARNESS
│   ├── manifest.py          # build_assistant() — wires tools+policy+audit+knowledge
│   ├── tools.py             # 15 chat tools; EXCLUDED = absences, not gates (R26)
│   ├── operator_tools.py    # 14 operator tools for the agent/CLI surfaces (pull, draft, status…)
│   ├── config_policy.py     # CONFIG_WRITABLE allow-list; FROZEN = computed complement
│   ├── profile_policy.py    # PROFILE_WRITABLE allow-list; identity + CV frozen by complement
│   ├── sink.py · card.py    # one audit sink, one confirmation card, four renderers
│   ├── knowledge.py         # postings → FTS5 chunks, all Trust.UNTRUSTED
│   └── evalset.py           # labeled cases: selection / grounding / in-bounds
├── mcp/                     # THE MCP OPERATOR SERVER — fourth renderer, stdio
│   ├── operator.py          # one owning thread for Store + GuardedToolBox + Auditor (R15)
│   ├── bridge.py            # Registration → MCP tool; policy → annotations; elicited confirmations
│   ├── resources.py         # personalagent:// resources served THROUGH execute() (audited)
│   ├── prompts.py           # server instructions + onboard/operate prompts
│   └── __main__.py          # python -m jobagent.mcp [--admin] [--check]
└── bot/assistant_bridge.py  # Telegram /ask — logic here, handler stays thin

src/agentkit/                # DOMAIN-AGNOSTIC HARNESS — never imports jobagent (R30)
├── tools.py                 # ToolBox: a raising tool becomes a result, not a crash
├── permissions.py           # READ/ACT/ADMIN + costly; UNIVERSALLY_EXCLUDED (R25)
├── guard.py                 # GuardedToolBox — same shape as ToolBox, so no bypass
├── audit.py                 # intent before policy; fail-closed sink (R27)
├── session.py               # SessionContext — valuable for what it LACKS (R28)
├── knowledge.py             # FtsIndex, Trust, nonce-fenced render()
└── llm/
    ├── types.py             # ChatRequest/Result IR + portable tool-schema validation
    ├── capabilities.py      # resolve_card(): measured → family → size → UNKNOWN
    ├── router.py            # choose_strategy() + plans_for() — the failover queue
    ├── strategies.py        # nine executors; prefetch_single_shot is load-bearing
    ├── runner.py            # walks the plan queue, classifies before acting
    ├── errors.py            # classify() by SDK type name — imports with no SDK
    ├── health.py            # per-(provider,model) breaker, injected clock
    ├── chain.py             # build_chain() — adding a key needs no code change
    ├── service.py           # LLMService — THE reusable entry point: chain + breaker
    │                        #   + ledger + preflight behind one object
    ├── probe.py             # concurrent pre-flight; routes by measured latency
    ├── ledger.py            # per-backend trace: works/dead, verdicts, latencies
    ├── jsonx.py             # tolerant JSON; every rule is an observed failure
    └── backends/            # openai_compat, anthropic_chat — SDK imported lazily

dashboard/src/
├── lib/api.ts               # API client, types, fetch functions
├── lib/assistant.ts         # SHARED assistant client — owns the session, renders the
│                            #   thread; the bubble and the page both just mount() it
├── styles/assistant.css     # one stylesheet for both surfaces (identical by construction)
├── components/AssistantBubble.astro  # floating chat on every page except /assistant
├── layouts/Layout.astro     # Shared layout (portfolio dark theme, .topbar not .bar)
└── pages/
    ├── index.astro          # Overview: stats, funnel, analytics, timeline, top matches
    ├── jobs.astro           # Filterable job list with pagination
    ├── jobs/[id].astro      # Job detail with fit-check button
    ├── applications.astro   # Application tracker with inline status editing
    ├── assistant.astro      # Assistant chat + confirmation cards (nonce-only)
    └── settings.astro       # Auth-gated config editor (LLM/Telegram/SMTP)

scripts/                     # CLI entrypoints: run_bot, run_api, pipeline, apply, etc.
                             #   ask.py (assistant CLI), llm_doctor.py (routing, offline),
                             #   eval_assistant.py (eval + floors), genkey.py (Fernet key)
deploy/                      # systemd service/timer units
config/preferences.toml      # User profile, watchlist, sense toggles
```

---

## Adding a New Sense

1. Create `src/jobagent/ingestion/adapters/myboard.py`.
2. Subclass `BaseAdapter`. Set `.source` to a new `Source` enum value.
3. Implement `fetch(settings) -> list[JobPosting]`. Normalize all fields. Store the
   raw payload in `JobPosting.raw`.
4. Add the `Source` enum value to `core/schemas.py`.
5. Register in `ingestion/registry.py` `build_adapters()`.
6. Add a toggle to `preferences.py` `Sources` model.
7. Write tests in `tests/test_ingestion.py` (mock HTTP, verify normalization + dedup hash).

## Adding a New LLM Provider

1. If OpenAI-compatible: add an entry to `_PROVIDERS` in `llm_client.py` with the
   `base_url`. Add `{provider}_api_key` and `{provider}_model` fields to `Settings`.
2. If custom protocol: subclass alongside `AnthropicBackend` in `llm_client.py`.
3. Add to `_DEFAULT_ORDER` in `llm_client.py`.
4. Add to `MANAGED_FIELDS` in `secrets_store.py` so the dashboard can configure it.

---

## Adding a New Assistant Tool

1. Write the function in `src/jobagent/assistant/tools.py`. It takes one `dict` of
   validated arguments and returns a **string** — that is what a model reads.
2. Call the service layer **in-process**. Never this system's own REST API: a URL can
   be redirected by a string the model emits, a named Python function cannot.
3. Give it a `ToolPolicy`. READ never confirms; ACT confirms once per session; ADMIN
   confirms every time. `costly=True` is orthogonal — a tool can change nothing and
   still need a budget, and a prompt would just train the operator to click through.
4. Register it with `Registration(spec, run, policy)` in the returned list. The schema
   must pass `validate_tool_schema` — flat objects, primitives, one nesting level.
5. **Read the store keys, do not remember them.** Wrong keys render `None` into
   model-visible text (R32). Query wider than you display so a cap is never reported
   as a total.
6. If the tool must never exist, add its name to `EXCLUDED` instead of writing it (R26).
7. Add a case to `assistant/evalset.py` if a real question should reach it.
8. Declare `surfaces=` on the Registration. Chat tools leave it None; operator actions
   declare {AGENT, CLI} so Baer's per-turn schema cost does not grow.

**Before adding an ADMIN tool, ask whether the action moves data or grants access.** If
it does, it belongs in the frozen complement, not `CONFIG_WRITABLE`.

---

## Common Pitfalls (from build history)

| Pitfall | What happened | Fix |
|---|---|---|
| Substring matching | "Go" matched "going", "RAG" matched "fragment" | Word-boundary regex in `_hits()` |
| Single-company flood | 8 Replit jobs filled top-10 | `diversify()` per-company cap |
| Blank env vars on CI | `TELEGRAM_CHAT_ID=""` → int parse crash | `field_validator` coercing blank to None |
| CSS class collision | `.bar` used for header AND chart bars → header collapsed to 8px | Renamed header to `.topbar` |
| SQLite cross-thread | Bot shared Store across event-loop and workers → crash | Per-thread Store open/close |
| CAPTCHA false positive | Scanning HTML string for "turnstile" matched shipped JS | Check rendered DOM elements via selectors |
| Settings overlay int | UI stores numbers as strings; `model_copy` skips validation | Explicit int coercion before overlay |
| `__future__ annotations` | Dep injection saw string "Request" not the class → 422 | Removed future-import from affected file |
| Unauthenticated writes | `/apply/{id}/approve` returned **200** to an anonymous caller — it would have sent email as the user | `dependencies=auth` on every non-GET route + a route-table test that fails if one is missed |
| Browser fetching `127.0.0.1` | `define:vars` baked the server-side URL into client JS; worked locally, broke any split deploy | `publicApiBase()` / `PUBLIC_JOBAGENT_API_URL` |
| Retry claimed, never implemented | R8 promised backoff; senses called `client.get` once, so a transient 429 lost that whole sense | `get_with_retry` in `ingestion/util.py` |
| Silent pipeline death | A three-day-dead pipeline rendered identically to a healthy one | `pipeline_health()` + dashboard stale banner + digest `health_banner()` |
| Guessed store keys | Assistant tools rendered `jobs=None ... (Noneh ago) stale=None` because key names were written from memory, not read off `Store`. A model handed `None` states it as fact or invents around it | Four renderers corrected; a test now runs every read tool against a *populated* store and fails on any `None` in model-visible text (R32) |
| Cap reported as total | `top_matches` queried `limit=MAX_ROWS`, making the cap indistinguishable from the count — it answered "there are 12 strong matches" when there were 231 | Fetch wider than you show (`FETCH_ROWS > MAX_ROWS`), assert the gap (R32) |
| `tool_use_failed` as CAPABILITY | Groq's 400 for a malformed call was classified "this model cannot use tools" — about a model measured 5/5 on loops. It never retried and blamed the wrong thing | Classified TRANSIENT; generation is non-deterministic and the attempt budget bounds the retry |
| Dead `:free` model default | `meta-llama/llama-3.3-70b-instruct:free` began 404ing; the third provider in the chain was dead on every call, silently | Verified live and replaced. `:free` slugs are withdrawn without notice — re-verify when the chain looks short |
| Partial save blanked the rest | Saving one Settings tab wrote every profile field's default (name=""…) via model_dump(), shadowing the real values in the lower layers. Found in a browser. | model_dump(exclude_unset=True): persist only the keys sent (R32-adjacent) |
| Two readers for one setting | `SecretStore` read `os.environ` while everything else read pydantic-settings, which does not export `.env`. Reads worked (no store file → no crypto), every *write* failed with "not set" — while it *was* set | One reader per value; fall back through the same parser pydantic uses, keyed on presence not truthiness (R31) |
| Test read the developer's machine | `test_preferences_load` asserted a name that only the gitignored identity overlay supplies — green locally, red in CI, always | Pin `local_path` at a nonexistent file; a guard test forbids bare `load_preferences()` in tests |
| Vacuous eval pass | The assistant eval printed "grounding 100%" over **zero** graded cases. A number that reassures without measuring is worse than none | Rate is `None`, not `1.0`; the table prints `n/a` |
| Eval failed a correct answer | The model wrote `12,971`; the store said `12971`. A right answer scored as a miss sends you hunting a bug that is not there | Normalize digit separators on both sides before matching |
| Fixed prefetch, wrong questions | `prefetch_single_shot` always fetched health + recent runs, so on the degraded path the model never chose a tool — every other question was unanswerable. Measured 50% | Question-aware keyword routing in Python; re-measured 100% |
| Tiny probe said "reachable" | `llm_doctor --probe` reported a provider healthy when it could not serve a real request — "reply ok" fits where a system prompt plus 15 tool schemas does not | Probe at realistic size too; a doctor consulted when things are broken must not say they are fine |
| 1 MB job list | `/jobs` shipped `raw` — the untouched source payload — on every row: 63% of the response, ~640 KB per dashboard page load, read by nobody | Stripped on the wire; the store still keeps it. Storage rule ≠ transport rule |
| …then 3 MB | Removing the per-company cap and fetching 400 rows re-exposed the same defect through a different field: `description` was 95% of the list payload (136 KB of 143 KB over 20 rows) for text the list never renders. 3.0 MB → 346 KB | `description` joined `_WIRE_OMIT`; `/job/{id}` still serves it. **Widening a query re-prices every field on it** |
| Digest cap on a browse list | The dashboard reused `ranked_matches`, whose `diversify(max_per_company=2)` is right for a bot top-10 and wrong for a stash queue. 231 strong unstashed matches rendered as 46, while the badge beside them said 231 | `max_per_company=None` from `/jobs`. The existing parity test could not see it — every job in it had a distinct company, so the cap never bound |
| Guard placed after the thing it guards | `purge_jobs` refused an unfiltered delete by checking `if not where` — but the scored-ness predicate was appended to `where` *first*, so the list was never empty and the guard was dead code from the moment it was written. Caught by the test, not by review | Check user-supplied filters before adding JOIN-shape predicates. **A guard tested only through the path that populates its input never fires** |
| Deletion vs derived data | The FTS knowledge index is a persistent table refreshed only by full rebuild, so deleting jobs leaves chunks the assistant still retrieves and cites — a confident answer about a row that no longer exists | Any real purge drops `agent_knowledge`; it rebuilds on next use. Ask of every new delete path: *what else derived from this?* |
| Windowed default vs unwindowed count | `/jobs` defaulted to `within=7d`; `stats()["queue"]` has no date filter. With a stale pipeline the button promising 231 landed on "Nothing matches" | Default `within=any`, so the page and the badge answer the same question |
| Provider exhaustion as 500 | A free-tier daily limit surfaced as `Internal Server Error`, which reads like a code fault | 503 naming the cause and pointing at `make doctor PROBE=1`. Note `/fit` and `/match` correctly stay 200 — both fall back to heuristics |
| Import cycle via package `__init__` | `agentkit.tools` → `agentkit.llm.types` → runs `llm/__init__.py` → `runner` → `agentkit.tools`. Only failed depending on import order, so it looked fixed once | `Runner` duck-typed against the tool seam (better design anyway — it is why a governed box substitutes); topological-sort test over module-level imports |
| Test asserted the sequence, not the property | "Intent is audited before the policy runs" checked event *order*, which survives moving the call. It stayed green with the guarantee gone | Assert the consequence: a dead sink must stop the call *before* the gatekeeper is asked anything |
