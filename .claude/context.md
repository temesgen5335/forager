# Context — Personal Job Agent

## Problem Statement

Job hunting as a software engineer is a high-volume, repetitive, multi-platform grind.
You monitor dozens of Telegram channels and job boards, manually cross-reference each
posting against your skills and preferences, tailor CVs and cover letters per company,
fill out near-identical ATS forms, and track outcomes in a spreadsheet. The process
scales linearly with effort and burns hours that should go toward interview prep.

This project automates every step except the final human decision: **apply or skip**.

## Vision

A self-hosted, single-user autonomous agent that:

1. **Ingests** job postings from Telegram channels, remote job boards (RemoteOK,
   Remotive), and ATS platforms (Greenhouse, Lever, Ashby) on a schedule.
2. **Matches** them against a structured profile (skills, roles, experience,
   location preferences) using a heuristic pre-filter (always, no API cost) plus
   an optional LLM rerank for the top candidates.
3. **Surfaces** ranked results in a Telegram bot with interactive filters
   (date/location/keywords) and one-tap apply buttons.
4. **Generates** tailored application assets (CV variant, cover letter, email draft)
   using multi-provider LLM failover — never fabricating experience.
5. **Fills** ATS forms with a HITL browser-automation flow (Playwright), pausing
   for human approval before any submit.
6. **Tracks** applications through a lifecycle (matched → drafted → submitted →
   interview → offer/rejected) on a self-hosted dashboard.

The system is **reusable by anyone** — write your own `config/preferences.local.toml`
(identity) and `.env` (credentials), adjust `config/preferences.toml` (target roles,
skills, watchlist), and it works for a different person. No identity is hard-coded,
and the committed config carries placeholders only.

## End Product

Two interfaces, one backend:

- **Telegram bot** — primary daily interaction. `/menu` with filters, `/jobs`,
  `/apply <rank>` with fit-check + approval gate, `/status`.
- **Astro SSR dashboard** — analytics (funnel, rates, timeline), job detail with
  on-demand fit breakdown, application status editing, LLM/Telegram/SMTP
  configuration via an auth-gated settings page.
- **FastAPI orchestrator** — sole backend. Wraps the service layer (ingestion,
  matching, apply, fit, LLM, secrets). Both bot and dashboard are its clients.

## Current State (v2 complete, July 2026)

| Component | Status |
|---|---|
| Ingestion (7 adapters) | Done — RemoteOK, Remotive, Himalayas, Greenhouse, Lever, Ashby, Telegram (+ JSearch aggregator, key-gated) |
| Matching (heuristic + LLM) | Done — word-boundary scoring + optional LLM rerank |
| Telegram bot | Done — interactive /menu, /apply with fit-check, ATS path |
| Tier-1 apply (email) | Done — CV tailor + cover letter + SMTP send with HITL gate |
| Tier-2 apply (ATS form-fill) | Done — Playwright fill + screenshot preview + submit gate |
| FastAPI orchestrator | Done — 15+ endpoints, auth, injectable deps |
| Dashboard | Done — overview analytics, filtered job list, job detail + fit, settings |
| Multi-LLM failover | Done — Groq/OpenRouter/Gemini/OpenAI/Anthropic + custom endpoint |
| Encrypted config UI | Done — Fernet secret store, auth-gated API, masked reads |
| API auth on writes | Done (Tier 1) — bearer token on every non-GET route, fails closed |
| Pipeline health | Done (Tier 1) — staleness + error count + per-source freshness, dashboard banner, digest warnings |
| Source retry/backoff | Done (Tier 1) — bounded jittered backoff, capped Retry-After |
| PII split | Done (Tier 1) — identity in gitignored overlay; committed config is placeholders |
| History scrub | Done (2026-07-30) — CV blob + phone/email removed from all 37 commits via filter-repo |
| Status lifecycle | Done (Tier 2) — transition map enforced, audited correction override |
| Weighted matching | Done (Tier 2) — skill weights, role-zone tiers, seniority + must-have checks |
| Gap surfacing | Done (Tier 2) — gaps as chips in the dashboard job list |
| Follow-up reminders | Done (Tier 2) — quiet-application list + drafted nudges (never sent) |
| Run-ID spine | Done (Tier 3) — one id ingest→match→digest, run ledger, GET /runs + /runs/{id} |
| Matching eval harness | Done (Tier 3) — 24 labeled traps, P@5=1.0 / P@10≥0.9 floors, tuning CLI |
| Architecture doc | Done (Tier 3) — rewritten around the real system; legacy Hermes diagram gone |
| Ingest lock (M5) | Done — SQLite advisory lock w/ 2h TTL; second pass exits, API returns 409 |
| CI | Done — tests.yml runs the offline suite + F821 gate on every push/PR |
| Dependability (Phase 0) | Actions workflow hardened: ingest gate applied in CI, store pruned+vacuumed so the 10GB cache limit can't be hit, failure notifies Telegram. Silence alarm warns when a schedule was skipped. **Still needs repo secrets set to actually run.** |
| Store retention | Done — `prune_jobs(older_than_days)`; jobs you applied to are never pruned |
| Provider coverage | groq · gemini · openai · anthropic · qwen · openrouter · custom, all assembled by `build_chain(settings)`. Families resolve by pattern and open models by parameter size, so adding a key needs no code change |
| agentkit (Phase 1) | **Complete.** `src/agentkit/`: chat+tools IR, provider translation, measured tier registry, classified errors, circuit breaker, router (`plans_for`/`choose_strategy`), the nine strategy executors, the `ToolBox` seam, tolerant JSON, and the `Runner` that walks the plan queue. Verified live: the same task answered correctly through `native_loop` on llama-3.3-70b and through `prefetch_single_shot` on llama-3.1-8b (measured incapable of a tool loop), and a full failover walk 401→429→answer across three providers |
| Triage respected everywhere | Done — dismissed/snoozed jobs leave the digest, bot `/jobs` and `/apply` numbering; the dashboard opts out (`hide_triaged=False`) so it can still render Undo |
| Triage | Done — dismiss/snooze/note per job (triage table, POST /triage, queue count) |
| Queue parity | Done (Aug 2026) — the number the badge shows and the rows `/jobs` renders are now the same set. `/jobs` passes `max_per_company=None` (the bot keeps its cap) and the page defaults to `within=any`. Verified live: 231 = 231 |
| Manual ingestion trigger | Done (Aug 2026) — "Pull Jobs" on the Overview calls `POST /ingest`, then polls `/runs/{id}` for per-source progress and reloads. Until this, nothing in any UI could start a pass, and no scheduler is live (see below). Verified live: 8,363 fetched across all 6 adapters, zero errors |
| Standalone LLM service | Done (v3.7.0) — `agentkit.llm.LLMService`: chain + breaker + trace ledger + concurrent pre-flight behind one duck-typed object. Verified usable with `jobagent` absent. `make doctor HEALTH=1` |
| Provider coverage (live) | groq · cerebras · gemini · github · openrouter · qwen · custom · openai · anthropic. **Verified live Aug 2026**: groq `openai/gpt-oss-20b` 0.84s, openrouter `gpt-oss-20b:free` 4.16s, gemini `gemini-flash-latest` 9.06s. Both previous defaults were dead |
| Inbox outcomes | Done (v3.6.0) — optional IMAP scan PROPOSES interview/offer/rejected for one-tap confirmation; never applies one. Obeys `ALLOWED_TRANSITIONS`, audited with `source: "inbox"`. `make inbox`. **Classifier and attribution tested against fixtures; never run against a real mailbox** |
| Bot handler coverage | Done (v3.6.0) — fake `Update`/`Context` harness in `tests/test_bot_handlers.py`. Closes the gap that let an undefined `_llm()` ship in `/apply`. Covers the owner gate, every command, malformed args, and a no-`None` check |
| LLM usage accounting | Done (v3.6.0) — calls/failures/estimated tokens per provider on the run ledger. Failures counted, because a dead first backend is invisible when the answer still arrives from the next |
| Aggregator source | Done (v3.5.0) — JSearch adapter (LinkedIn/Indeed/Glassdoor/ZipRecruiter via RapidAPI). Queries come from `target_roles`; self-gates on key AND queries. Needs `JSEARCH_API_KEY` + `[sources] aggregator = true`. **Built and tested against fixtures; never run against the live API** — no key available |
| Cross-board clustering | Done (v3.5.0) — `cluster_key` groups the same role across boards WITHOUT touching `dedup_hash`, which is the PK every application references. Dashboard shows "also on N" |
| Salary | Done (v3.5.0) — parsed to columns at write time, chip in the list, annualised min-salary filter. Unknown pay is KEPT by the filter (most postings state none) |
| Score provenance | Done (v3.5.0) — `score_source` + `llm_score`; the store COALESCEs so a heuristic re-run cannot erase a rerank. Closes a July 2026 audit gap |
| Exposure controls | Done (v3.4.0) — `JOBAGENT_REQUIRE_AUTH_READS` gates every GET except `/health` (route-table test enforces it), the API refuses to start with read-auth and no password, per-client rate limits on assistant/ingest/write classes return 429 with Retry-After, and both deployment docs open with the warning |
| First-run onboarding | Done (v3.3.0) — `make setup` wizard (pure logic in `jobagent.setup_wizard`, merges `.env` key-by-key so nothing hand-tuned is lost), `make demo` seeding a throwaway store, and containers (`Dockerfile`, `compose.yml`, `make docker_up`) |
| OSS packaging | Done (v3.2.0) — LICENSE, `preferences.example.toml` with a loader fallback, lockfiles committed, SECURITY/CONTRIBUTING/CoC, issue+PR templates, and `tests/test_packaging.py` pinning all of it |
| Job cleanup | Done (Aug 2026) — `purge_jobs()` + `POST /jobs/purge` + a preview→confirm panel on /jobs. Filters shared with the list via `_row_predicates`, `dry_run` default true, unfiltered purge refused, applications/CVs/notes spared unconditionally, knowledge index dropped on delete |
| Shared sign-in prompt | Done (Aug 2026) — `window.JA.signIn()` in `Layout.astro`: any page's 401 raises a password modal and retries once. Writes the same `jobagent_token` the Settings page does, so one session covers both. 403 (no `DASHBOARD_PASSWORD` on the API) is reported as its own case, never as a wrong password |
| Ingest gate | Done — age/locations/drop-keywords + source selection, editable in Settings, applied before storage with per-reason drop counts |
| Dashboard v3 | Done — sidebar shell, health-first Overview, triage queue + focus mode, fit-check states, nudge banner, locked Settings (from the Claude Design project) |
| agentkit (Phase 2) | **Complete.** Permission tiers (READ/ACT/ADMIN + structural exclusion), argument-bound single-use confirmations, FTS5 knowledge index with per-chunk provenance and trust, fail-closed audit on the run_id spine, and `GuardedToolBox` — same shape as `ToolBox`, so it drops into the Runner and there is no ungoverned path |
| assistant (Phase 3) | **Complete.** `src/jobagent/assistant/`: 15 chat tools, R2 exclusions as absences (no send/approve/ats tool exists), `CONFIG_WRITABLE` allow-list with frozen as the computed complement, impact previews dry-run over real stored rows, config snapshots + rollback, and FTS5 search over postings fenced as UNTRUSTED |
| assistant interfaces (Phase 4) | **Complete + extended.** Four surfaces on one mechanism: `scripts/ask.py` (CLI), the `/assistant` dashboard page, a floating chat **bubble on every page** (`components/AssistantBubble.astro`), and Telegram `/ask`. The bubble and the page share one client (`lib/assistant.ts`) and one `localStorage` session, so a conversation continues seamlessly between them until cleared with New chat. Confirmations differ only in renderer — the CLI binds to `sha256(args)`, HTTP and Telegram send only a nonce and keep the arguments server-side. Config writes are refused on chat by construction (`Surface.CHAT` is outside `admin_surfaces`) |
| assistant hardening (Phase 5) | **Complete.** 10-case eval set scoring tool *selection*, answer *grounding* and *in-bounds* separately; `scripts/eval_assistant.py` with floors; `scripts/llm_doctor.py` explaining the chain, every model card's provenance, and per-task routing offline. Degraded-path conformance run measured **100% / 100% / 100%** |
| MCP operator server | **Done (v3.8.0).** `src/jobagent/mcp/`: the governed toolbox over stdio for Claude Code / Codex; 14 operator tools (pull_jobs, rematch, list_matches, company_dossier, fit_check, draft_application, set/correct_application_status, annotate_job, setup_status, current_profile, lifecycle, propose/apply_profile_change) hidden from chat via `Registration.surfaces`; confirmations via SDK resolvers with the Gatekeeper nonce underneath; ADMIN hidden unless `--admin`; every call on the run ledger under one session run id. `make mcp_check` |
| Lifecycle + pass seams | Done (v3.8.0) — `lifecycle.transition()` (two routes + the tool) and `pipeline.run_pass()` (API task, scheduled script, agent); API-triggered passes now write a `run` row tagged `trigger: api` |
| Profile & preferences | **Editable through the UI.** Identity, background, CV, search preferences, source toggles and the ATS watchlist all persist to a gitignored `data/profile.json` + `data/cv_master.md` overlay (three-layer merge: committed placeholders → legacy `preferences.local.toml` → writable overlay). `/profile` GET+PUT (both auth-gated — PII). Nothing personal is hardcoded; the tree carries placeholders only (R22) |
| Settings UI | Tabbed: Profile · CV & background · Search & matching · Sources & watchlist · Ingestion · LLM · Telegram · Email. Each tab saves independently against the backend that owns it (`/profile` or `/config`) |
| Test suite | 821 tests, 61 test files, zero network, injectable fakes throughout |

## Assistant cost characteristics (measured Aug 2026)

One `native_loop` turn sends ~1,258 tokens before any tool result — **1,047 of them the
15 tool schemas, resent on every turn**. A 5-step answer therefore costs ~8k tokens,
against ~350 for the same question on `prefetch_single_shot`. Not a defect, but it is
why free-tier daily budgets drain quickly, and it is the number to attack first if cost
becomes a concern (trim the tool set per turn — `GuardedToolBox.allowed` already exists
for exactly this).

## Known Gaps (from the July 2026 audit)

Tier 1 of the remediation roadmap is complete (API auth, PII split, browser API URL,
pipeline health, retry/backoff, docs truth-pass). Still open:

- **Dedup is weak for Telegram** — the hash is company+title+location and the
  Telegram parser never sets a company, so those postings dedup on the title line only.
- **Heuristic scores overwrite LLM scores** each run; no score provenance is kept.
- **Tag-driven role signal** lets a few postings score strongly on stack tags rather
  than the title (3 of 218 strong matches, all from one dev marketplace that tags its
  whole stack regardless of role). Left alone deliberately: tuning the scorer around
  one board's tagging habit would be overfitting.
- **FakeLLM tests cannot detect fabrication.** Two R1 violations survived a full green
  suite and were only caught by running the real model (see R1a/R1b).
- **Gemini's tool support is still unverified.** The card claims `native_tools=True`
  from the family pattern, but the free-tier quota has been exhausted on every attempt,
  so no call has ever reached it. The card's `notes` field says so; treat the claim as
  documented-not-measured until a call succeeds.
- ~~**The Telegram handlers have no runtime test coverage.**~~ **Closed in v3.6.0** —
  `tests/test_bot_handlers.py` is the harness this paragraph asked for. Kept below for
  the history of how the gap was found.
- (historical) `tests/test_bot.py` covers
  only the pure helpers in `bot/service.py`; the handlers in `bot/app.py` need live
  `Update`/`Context` objects. This is how a call to an undefined `_llm()` shipped in
  the `/apply` fit-check path and crashed it with `NameError`
  (fixed in Tier 1). `tests/test_static_checks.py` now guards that class of bug, but
  it is a static check, not real coverage — a handler harness is still missing.

## Where the gaps are tracked

**`docs/ROADMAP.md` is the authoritative list** of what is missing and which release
carries it, from the 2026-08-18 open-source review. The short version of what blocks a
new user today: there is no LICENSE file (so nobody may legally use it), and
`config/preferences.toml` ships the author's own search profile — roles, 37 skills,
tuned weights, a 40-company watchlist, and a real `location`/`timezone` — so a stranger
silently gets someone else's job search. Both are v3.2.0.

## What's NOT Built Yet

- **Aggregator adapter** (JSearch/SerpApi for Indeed/LinkedIn/Glassdoor) — toggle ready, no adapter code
- **Profile/watchlist editing** in dashboard Settings (currently file-based)
- **Bot outcome-marking** (interview/offer/rejected from Telegram — currently dashboard-only)
- **Deployment to a live VPS** — systemd units + scripts are ready, needs a provisioned box
- ~~Filtered job cleanup from the dashboard~~ — **built 2026-08-18**, see below
- **No ingestion is actually scheduled anywhere.** Both triggers exist and neither is
  live: the systemd timers (`deploy/jobagent-ingest.timer`, every 4h; `.pipeline.timer`,
  daily 07:00) need the VPS above, and `.github/workflows/digest.yml` (`0 4 * * *`) needs
  repo secrets set. The evidence is in the store — ingest events cluster on four
  scattered days (2026-06-17, 07-28, 07-30, 08-07), the signature of hand-run passes, not
  a schedule. Until one is live, **"Pull Jobs" and `make pipeline` are the only
  things that refresh the store.**

---

## Filtered job cleanup — BUILT (2026-08-18)

Planned 2026-08-17, implemented the next day. What shipped, against what was designed:

- **`Store.purge_jobs(...)`** — filtered delete, `dry_run=True` by default. Shares
  `_row_predicates()` with `get_matches()`, so the rows the dashboard lists and the rows
  a cleanup deletes cannot mean different things. `prune_jobs` is untouched, so
  `scripts/prune_store.py` and the Actions workflow still work.
- **`POST /jobs/purge`** — `dependencies=auth` (R19), `dry_run` defaulting to true, and
  a **400 on an unfiltered purge** rather than reading "no filters" as "everything".
  Returns fresh `stats` so the caller need not make a second round trip.
- **Jobs page** — a collapsed "Clean up stored jobs" panel: pick a preset (weak matches
  under N%, everything dismissed, not seen in N days, everything matching the current
  filters), Preview, then a separate explicit Delete. The page's live filter state is
  always sent, so a purge is scoped to the view you are looking at.
- **Spared unconditionally**: anything with an application, a tailored CV, or a triage
  note. Verified on the real store — the widest possible purge selects 14,295 of 14,296
  and spares exactly the one job carrying a note.
- **The knowledge index is dropped on any real delete**, because it is derived data that
  does not notice deletions and would otherwise let the assistant cite postings that no
  longer exist. It rebuilds on next use.

Deliberately still absent: **no assistant tool** (R26 — it destroys data, so it is not
registered rather than registered-and-gated), and **no VACUUM by default** (opt-in;
it rewrites the whole file).

Measured on the live store the day it shipped: under-50% would remove 13,412 of 14,296;
not-seen-in-60-days would remove 3,417.

## Key Numbers

- **11,700+** jobs scored in a live run (8,253 fetched in a single pass across 6 adapters)
- **40** companies in the ATS watchlist (Greenhouse/Lever/Ashby)
- **821** tests across 61 files — all run offline, no network, no credentials
- **6** LLM providers with automatic failover (3 free, 3 paid)
