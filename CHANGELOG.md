# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as
scoped in [docs/VERSIONING.md](docs/VERSIONING.md) — which is worth reading, because
"public API" means something specific for a self-hosted single-user agent.

## [Unreleased]

Planned work is tracked in [docs/ROADMAP.md](docs/ROADMAP.md), grouped by the release
that will carry it.

## [3.8.0] — 2026-10-03

### Changed
- **Rebranded to Forager / Baer.** The system is **Forager**; the assistant is **Baer**. The
  README leads with the keyless quickstart, the geo/eligibility wedge, and the
  senses/forage/hunt/scavenge/den/trail/stash dialect — and that dialect now runs through the
  code itself (logs, docstrings, UI / Telegram / CLI copy, and `make forage`). The rename is
  deliberately contract-safe: no API route, DB table/column, config key, or module path
  changed, so existing stores, `.env` files, and integrations keep working.
- **The pipeline's LLM router is now the reusable agentkit service — no more duplicate.**
  `jobagent/llm_client.py` was a second multi-provider router (its own registry, failover,
  usage ledger, fan-out) parallel to `agentkit.llm`, which the assistant already used.
  `build_llm(settings)` is now a thin adapter that returns an `LLMService` — one provider
  registry, one failover path, plus agentkit's circuit breaker and capability routing, for
  the pipeline too. `LLMService` gained a per-service `temperature` (from_settings kwarg) so
  scoring/generation keep their deterministic 0.3; the API recognizes provider exhaustion by
  the `AllProvidersFailed` type instead of scanning the message; the run ledger records
  agentkit's richer per-backend trace. `MultiLLM`/`LLMUsage` and the duplicate fan-out are
  deleted.

### Added
- **Config-driven schedule cadence** (`src/jobagent/scheduling.py`, `scripts/install_services.sh`,
  `.env`) — the autonomous systemd timers' cadence is now set in `.env`
  (`INGEST_EVERY_HOURS`, `DIGEST_AT`, `INGEST_JITTER_SEC`) and rendered into the unit files by
  the installer through a pure `OnCalendar` translator (`python -m jobagent.scheduling`), instead
  of hand-editing systemd calendar syntax. Defaults reproduce the prior every-4h ingest / daily
  07:00 digest. `make forage` is added as an alias of `make pipeline`. (GitHub Actions keeps its
  cron literal — the platform forbids variables in `on.schedule`.)
- **Keyless first run + guided onboarding** — `make quickstart` writes a ready `.env` (generating
  `DASHBOARD_PASSWORD` / `JOBAGENT_MASTER_KEY` without clobbering existing ones) and seeds a
  demo den so a fresh clone shows ranked matches with zero credentials; `make onboard` is a
  guided (and scriptable `--config` / `--example-config`) setup for profile, sources, LLM, email,
  and Telegram. Going live is `POST /demo/clear` + `clear_demo()`, which removes the demo rows and
  their dependents while sparing real data; `store.stats()` gains `demo` / `first_run` flags.
- **Repo / CI hardening** — the test workflow runs the suite on Python 3.11 / 3.12 / 3.13 with the
  full extras (so the tailored-CV PDF-render path is exercised, not skipped) and builds the Astro
  dashboard; least-privilege `GITHUB_TOKEN`, per-ref concurrency, and job timeouts. Added
  `.github/CODEOWNERS`; fixed the `SECURITY.md` advisory link and added a Supported-versions note.
- **Himalayas ingestion source** (`ingestion/adapters/himalayas.py`) — a remote-first board
  whose free JSON API ships an explicit `locationRestrictions` list per posting ("Worldwide",
  "United States", or a country set). That maps straight onto the `location` field, so the
  geo-eligibility scorer can tell a genuinely global role from a US-only "remote" one without
  guessing — the structured signal the company-watchlist boards lack. No API key; toggled by
  `[sources] himalayas`. Registered in the adapter registry and `ALL_SOURCES`; fixture-tested
  and verified live against the API. Brings the adapter count to seven.
- **Geographic-eligibility scoring** (`matching/heuristic.py`, `preferences.py`) — the
  heuristic now reads a posting's *work-location requirement*, not just whether the word
  "remote" appears, and it is **fully configurable — no geography or home region is hardcoded**
  (R22). `remote_scope="global"` keeps only genuinely global-remote postings and caps every
  place-pinned one (remote-US, remote-UK, a city — and the candidate's own country too) at 0.15
  like an exclusion, with a `region-locked` gap chip; `remote_scope="any"` (the default) leaves
  it off. Three optional profile lists shape it: `geo_global_terms` (what "globally open" means),
  `geo_eligible` (always-allow), and `geo_blocked` (always-demote, honoured in any scope). The
  place test is **gazetteer-free** — it strips the remote/global vocabulary from the location and
  locks it if any place name survives — so it needs no country list to maintain and handles
  arbitrary places (China, Uruguay, `Remote - CA`, `Remote - EMEA`) uniformly. A region
  requirement in the body ("authorized to work in the US") is caught even when the location says
  only "Remote". The eval set gains geo trap and configurable-include classes
  (`matching/evalset.py`); precision@10 is 1.0. Config lives in `config/preferences*.toml`
  (`remote_scope` + `geo_*`). Measured on the live store: with `remote_scope="global"` the
  strong-match queue fell from 311 to ~10 once place-locked jobs were demoted.
- **Geo scoring also reads the job title** (`matching/heuristic.py`) — some boards keep the
  location field global ("Distributed") but pin the role in the title ("… - Charlotte, NC").
  A high-precision `City, ST` check (real US state abbreviations, case-sensitive so a role
  qualifier like "ML"/"AI" after a comma never trips it) now locks those under global scope.
- **agentkit's reusable `LLMService` gains the live free-model fan-out** (`agentkit/llm/openrouter.py`,
  `agentkit/llm/chain.py`) — the domain-agnostic harness now carries the same capability as the
  app-side client, so any project embedding agentkit gets it. `openrouter.free_models()` is
  **stdlib-only** (urllib, injectable transport) — no new dependency, no host coupling (the
  import-boundary/vocabulary tests still hold). `build_chain` fans out over the live `:free`
  list when `openrouter_free_fanout` is set; `ProviderSpec` gains an `enabled_field` opt-in gate
  so keyless **Pollinations** stays off until asked. Added SambaNova, Nvidia, Mistral, Meta Llama,
  Pollinations to `DEFAULT_PROVIDERS`, and fixed the 404ing `openrouter_model` default. Verified
  live: `LLMService.from_settings(SimpleNamespace(...))` fans out over 6 free models and serves a
  real call — with `jobagent` absent from the path.
- **Smart multi-provider LLM router with live free-model discovery** (`llm_client.py`) —
  the failover chain is now table-driven off one `_PROVIDERS` registry and gains SambaNova,
  Nvidia (NIM), Mistral, Meta Llama, and keyless **Pollinations** (opt-in) alongside the
  existing Groq/Gemini/OpenRouter/Cerebras/GitHub/OpenAI/Anthropic — every one OpenAI-API
  compatible, activated by adding a key. **`OPENROUTER_FREE_FANOUT`** (default off) fetches
  OpenRouter's live `:free` chat-model list (`openrouter_free_models()`), ranks it
  (tool-capable + large-context first), and adds every model as a failover backend — so a
  withdrawn free slug (like the `gpt-oss-20b:free` that started 404ing) is simply skipped
  for the next working one, cached hourly, degrading to the configured model if the fetch
  fails. Verified live: of the top free models, gated ones 403 and the router falls through
  to `minimax-m3` / `nvidia/nemotron`. Keyless Pollinations is opt-in so a no-key install
  still reports "no LLM configured" rather than silently routing through a third party.
- **ATS-parseability report on every tailored CV** (`apply/verify.py`) — a pure,
  model-free check of the CV the way a résumé parser sees it: contact details present
  as literal text, no garbled glyphs (`(cid:…)` / `�`), and honest keyword coverage
  against the posting. Attached to every draft and surfaced in the API response, the
  Telegram preview, and `scripts/apply.py`. Read-only: it reports gaps, never stuffs
  keywords (R1). The technique is adapted from
  [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search) (MIT).
- **Optional PDF text-layer extractor** (`apply/pdf_verify.py`, vendored from the same
  project, MIT) — runs the identical report over a *rendered* CV's extracted text
  (`ats_report_for_pdf`), so the artifact actually attached to an application can be
  verified. pypdf → Poppler fallback; no PDF toolchain needed for the base install.
- **Markdown → PDF CV render** (`apply/render.py`, `APPLY_RENDER_CV_PDF`, default off) —
  closes the verify loop: with the flag on, the tailored CV is rendered to a PDF (fpdf2,
  pure Python — added to the `apply` extra), that PDF is what `approve_and_send` attaches,
  and the ATS report is run over *its* extracted text — so the report now describes the
  exact bytes that get sent, not just the Markdown. Single-column by design (a parser
  reads it cleanly); if the renderer is missing it degrades to verifying the Markdown and
  attaching the static `profile.cv_path`, never blocking a draft. Ships off: an
  auto-rendered CV is plainer than a hand-designed one, so keep your own PDF unless you
  want this.
- **Drafter → reviewer → revise loop** (`APPLY_REVIEW_ENABLED`, default off) — a second
  agent critiques the tailored CV and cover letter against the real CV and the posting,
  and the drafter revises. Both new prompts receive the CV (R1a) and re-assert the
  no-fabrication boundary (R1). It rewrites generated content, so it ships **off** and
  needs a live-model check before being trusted (R1b).
- **Skill-gap upskilling report** (`jobagent/upskill.py`, `GET /upskill`, Telegram
  `/upskill [fit]`, `scripts/upskill.py`, `make upskill`, and a governed assistant tool
  `upskill` — READ / Confirm.NEVER, reads recorded gaps, spends no quota, moves no data,
  R25–R29) — aggregates the gaps the matcher already records
  across scored matches, weights each by fit (a moderate match the candidate could close
  counts more than a weak one), and ranks the recurring ones into a heatmap; non-skill
  filters (seniority, location, hard-exclusions) are reported separately. With an LLM key
  set, the CLI also prints a prioritized now/next/later learning plan. Aggregation is pure
  and offline; the plan is the only model-backed step. Borrowed in spirit from
  [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search) (MIT).
- **MCP operator server** (`src/jobagent/mcp/`) — a coding agent (Claude Code, Codex, any
  MCP client) can operate the system over stdio through the *same governed toolbox* the
  chat assistant uses: pull jobs, re-run matching, list and sort matches, fit-check, research a
  company from the store, triage and annotate, draft an application (never send), and move
  applications along the lifecycle. Confirmations are form-mode elicitations answered by a
  person, bound to `sha256(args)` by the Gatekeeper underneath; ADMIN tools are hidden unless
  `--admin`; every call is on the run ledger under one session run id. Fourteen operator tools
  live in `assistant/operator_tools.py` and are hidden from chat surfaces. `.mcp.json` is
  committed for Claude Code; `make mcp` / `make mcp_check`. Offline tests drive the server
  through the SDK's in-memory client.
- `jobagent.lifecycle.transition()` and `jobagent.pipeline.run_pass()` — the one status-move and
  the one ingest→match→summary seam; API-triggered passes now appear in `GET /runs`.
- `PROFILE_WRITABLE` (`assistant/profile_policy.py`) — search preferences the agent may change,
  with a preview, a snapshot, and identity + CV frozen by complement.

## [3.7.0] — 2026-08-20

*Theme: the multi-LLM layer becomes a service you could lift into another project.*

### Added
- **`agentkit.llm.LLMService`** — one object instead of six modules. Owns a chain, a
  circuit breaker, a trace ledger and an optional pre-flight probe:
  `LLMService.from_settings(cfg).complete(system, user)`. Duck-typed on the config
  object, so it works with a pydantic Settings, a dataclass, or a `SimpleNamespace` —
  and `from_providers()` takes an explicit provider table for a host that does not want
  ours. Verified importable and usable with `jobagent` absent from the path.
- **Concurrent pre-flight** (`agentkit.llm.probe`) — calls every backend at once, then
  routes by measured latency instead of a static preference order. Reordering changes
  sequence, never membership, so a probe can never leave the caller with an empty chain.
- **Provider trace ledger** (`agentkit.llm.ledger`) — per-backend calls, failures,
  latencies, and failures grouped by classified verdict, with `working()` / `broken()`
  and a terminal-friendly `render()`. An untried backend reports `None`, not 100%.
- **Cerebras** and **GitHub Models** as first-class providers, wired through the chain,
  Settings, and the encrypted config store.
- `make doctor HEALTH=1` — the concurrent probe plus the health table, in seconds.

### Fixed
- **Groq and Gemini were both dead in the shipped defaults.** `llama-3.3-70b-versatile`
  had been withdrawn from Groq's catalogue and `gemini-2.0-flash` retired; every call
  paid two 404s before OpenRouter answered. Replaced with models verified against each
  account's own `/models` list: `openai/gpt-oss-20b` and `gemini-flash-latest` — an alias
  rather than a pin, because a pinned version is what died.
- A test asserted the dead Groq slug by name. It now asserts the *configured* model
  appears, so a model rotation cannot fail a test about the doctor.

### Documentation
- **`src/agentkit/README.md`** — a complete guide to using the harness in another
  project: every provider and its config attribute, pre-flight and the ledger, error
  verdicts, tool registration, the governed toolbox, retrieval, and a module map. It
  lives *inside* the package so vendoring the directory carries its own docs.
  `tests/test_agentkit_readme.py` executes every example — writing it from memory
  produced two that could never have worked (`c.retry_after` instead of `retry_after_s`,
  and a tool schema missing the per-property `description` the validator requires).

## [3.6.0] — 2026-08-20

*Theme: the tracker learns what came back, and the bot is finally tested.*

### Added
- **Inbox outcome detection** (optional, needs IMAP credentials). Reads the mailbox you
  apply from and **proposes** status changes — interview, offer, rejected — for one-tap
  confirmation. It never applies one: a wrong automatic transition corrupts the
  operator's own history silently, so accepting is an explicit action gated exactly like
  sending (R2). Accepting obeys the same `ALLOWED_TRANSITIONS` map a manual edit does,
  and is audited with `source: "inbox"` so a detected outcome stays distinguishable from
  a typed one forever.
  `GET /inbox/proposals`, `POST /inbox/proposals/{id}`, `make inbox`.
- **Runtime coverage for the Telegram handlers** — a fake `Update`/`Context` harness, the
  `FakePage` pattern applied to python-telegram-bot. `bot/app.py` previously had none,
  which is how a call to an undefined `_llm()` shipped in the `/apply` path. 18 tests
  covering the owner gate, every command, malformed arguments, and a no-`None` check.
- **LLM usage accounting** — calls, failures and estimated tokens per provider, recorded
  on the run ledger. Failures are counted too: a chain whose first backend is dead is
  otherwise invisible because the answer still arrives from the next one, which is
  exactly how two dead model slugs went unnoticed.

### Changed
- `list_applications` returns `job_id` and `apply_email`, which inbox attribution needs.

### Notes
- Detection is deliberately conservative. A reply is attributed by sender domain or by
  company name, and matches nothing when unsure — attributing a rejection to the *wrong*
  application would close out a live opportunity on the operator's record. Only
  `submitted` and `interview` applications are candidates.
- Token counts are **estimates** from character length, and say so in every key name.
  The backends return a string and nothing else, so real counts would mean changing
  every provider's return type. A guess presented as billed usage gets trusted.

## [3.5.0] — 2026-08-19

*Theme: more of the market, and less of it duplicated.*

### Added
- **JSearch aggregator adapter** — LinkedIn, Indeed, Glassdoor and ZipRecruiter behind
  one API. An aggregator rather than a scraper: those boards are aggressively anti-bot
  with no public API, so scraping them would be fragile and against both their terms and
  R7. Queries come from the profile's own `target_roles`, and the adapter self-gates on
  having *both* a key and something to search for — an empty query would return an
  arbitrary slice of every job on the internet. Needs `JSEARCH_API_KEY` and
  `[sources] aggregator = true`.
- **Cross-board clustering** — a `cluster_key` alongside the primary key groups the same
  role seen on several boards ("Senior Backend Engineer" / "…(Remote)" / "…— Berlin" at
  Acme Inc./ACME/Acme Technologies are one cluster). The dashboard shows "also on N".
  Deliberately **not** a change to `dedup_hash`: that is the primary key every
  application and triage row references, and redefining it would re-id the store and
  orphan the operator's history — a MAJOR by this project's own policy.
- **Salary parsing and filtering** — `salary_text` has been stored since v1 and never
  read. It is now parsed into `salary_min/max/currency/period` at write time, shown as a
  chip, and filterable by an annualised floor, so an hourly rate and a salary are judged
  on the same scale. The parser refuses to guess: equity percentages, team sizes and
  "5+ years of experience" return nothing rather than a wrong number.
- **Score provenance** — `score_source` and a separate `llm_score` column. The store
  COALESCEs rather than overwrites, so a heuristic re-run can no longer erase a rerank
  that cost real quota. This closes a gap open since the July 2026 audit.
- `get_with_retry` accepts `params` and `headers`, so an authenticated source still goes
  through the shared retry/backoff instead of calling `client.get` directly (R21).

### Changed
- Store columns are added automatically on open (`Store._migrate`), so upgrading an
  existing store needs no manual step — which is what keeps this release MINOR.

### Notes
- A minimum-salary filter deliberately **keeps** postings whose pay could not be parsed.
  Most postings state no salary, so dropping unknowns would hide the majority of the
  market behind a filter the operator thinks is about money. The active-filter chip says
  "(or unstated)" so the result is never read as "everything here pays this much".
- Embedding-assisted matching is **deferred**, and the roadmap's cost estimate for it was
  wrong: `sentence-transformers` pulls `torch`, not "~80 MB". See ROADMAP item 14 for
  three lighter alternatives.

## [3.4.1] — 2026-08-19

### Fixed
- **CI was red on `main` since v3.2.0.** Untracking `config/preferences.toml` broke six
  tests that could only fail where the file is absent — every one of them passed on a
  developer machine, which is where they were run.
  - `current_config` rendered `telegram_chat_id = None` into model-visible text. Integer
    settings coerce a blank env var to `None`, so any install without those values got a
    literal `None` — the R32 failure the guard beside it exists to catch, invisible on a
    machine with a populated `.env`.
  - Three tests read `config/preferences.toml`, which no longer exists in a clone.
  - `CLAUDE.md` and `AGENTS.md` cited it as a committed path.
  - `test_preferences_load` asserted the author's roles and ATS watchlist, which only
    the gitignored file supplies. It now compares against the committed template, so it
    cannot drift.
- `test_a_clone_with_no_overlay_still_yields_a_usable_profile` passed by coincidence —
  the path it named happened to equal `DEFAULT_PATH`, so the loader's fallback supplied
  the template regardless. It now names the template.
- The hermeticity guard only caught a bare `load_preferences()`, so pinning `local_path`
  while leaving the base at its default sailed through — the same bug one argument along.
  It now requires the base to be named, and its own `"path=" in args` check matched the
  substring inside `local_path=`, which made the first fix pass every offender it was
  written to catch.

## [3.4.0] — 2026-08-18

*Theme: the deployment the docs teach is no longer the one that leaks.*

### Added
- **`JOBAGENT_REQUIRE_AUTH_READS`** — opt-in authentication on GET routes. Off by
  default, which stays correct on the `127.0.0.1` bind; turn it on for any deployment
  the network can reach. `/health` stays open even then, because it is a liveness probe
  (the Docker `HEALTHCHECK` calls it) and reports nothing about the job search.
  A route-table test asserts every other GET is gated, so one added next year without
  `dependencies=read_auth` fails in CI rather than leaking quietly.
- **`scripts/api_token.py`** and `JOBAGENT_API_TOKEN` — the dashboard renders reads
  server-side and has no browser session to borrow, so it carries a derived token.
- **Per-client rate limits** on assistant/LLM calls (60/h), ingestion (20/h) and writes
  (600/h), returning `429` with `Retry-After` and naming the env var that raises it.
  Reads are deliberately unlimited: the dashboard makes several per page load, and a
  limiter that throttles normal use is one that gets switched off.
- `JOBAGENT_MAX_PURGE_ROWS` as a safety valve, defaulting to unlimited — the purge UI
  already shows an exact count and requires a second click, so consent is obtained
  before the delete and a cap would only add friction.
- Exposure warnings at the top of `docs/DEPLOYMENT.md` and
  `docs/DEPLOYMENT_ALTERNATIVES.md`, where the split-deploy path is taught.

### Changed
- The API **refuses to start** if read auth is on without `DASHBOARD_PASSWORD`. No token
  would exist, so every page would 403 forever — which reads as a broken app rather than
  a missing setting.
- The dashboard distinguishes "the API is up and refusing me" from "the API is down".

## [3.3.0] — 2026-08-18

*Theme: fifteen minutes from clone to first ranked job, without editing TOML by hand.*

### Added
- **`make setup`** — an interactive first-run wizard. Writes `.env` and your profile
  overlay, generates `JOBAGENT_MASTER_KEY` and (if you want) a dashboard password.
  Safe to re-run: `.env` is merged key-by-key, so comments and anything you tuned by
  hand survive, and unanswered prompts never overwrite an existing value.
  Logic lives in `jobagent.setup_wizard` as pure functions; only `scripts/setup.py`
  touches stdin, so it is tested without driving a terminal.
- **`make demo`** — seeds `data/demo.db` with 40 fictional postings, strong matches, a
  triaged row and three applications, so every dashboard page has something to render
  before you commit any credentials. It refuses to write into a store that already has
  jobs, and every seeded posting is marked as demo data in its description.
- **Containers** — `Dockerfile` (API, bot, CLI), `dashboard/Dockerfile` (Astro SSR),
  `compose.yml`, and `make docker_up` / `make docker_down`. Host ports bind to
  `127.0.0.1` only, because GET routes are unauthenticated. Playwright is opt-in via
  `--build-arg WITH_BROWSER=1` rather than a ~400 MB default.
- README now opens with a five-command fast path and a Docker section.

### Fixed
- The `make test` target described a "99-test offline suite".

## [3.2.0] — 2026-08-18

*Theme: a stranger can legally use this, and gets their own job search rather than mine.*

### Added
- **`LICENSE`** (MIT). `pyproject.toml` had claimed MIT for months with no license file,
  which legally means all rights reserved — nobody could use the code.
- **`config/preferences.example.toml`** — a neutral, commented template. The loader falls
  back to it, so a fresh clone runs before it is edited.
- **`scripts/check_profile.py`**, wired into `make check`: warns while the search profile
  is still template values, naming the fields.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue forms, and a PR template.
- `uv.lock` and `dashboard/package-lock.json` are now committed.
- `tests/test_packaging.py` — nine invariants covering every finding above.

### Changed
- **`config/preferences.toml` is no longer committed.** It carried a real search profile:
  location, timezone, 9 target roles, 37 core skills, 26 tuned skill weights and a
  40-company watchlist. Identity had been scrubbed to placeholders long ago, but that
  work framed "personal" as contact details — a PII scrub is not a personalization scrub.
- README truth-pass: profile setup now describes the Settings UI (the real path since
  3.0.0), the CV lives at `data/cv_master.md`, and the "nothing is hard-coded to one
  person" claim is replaced with one that is true.
- `.gitignore` uses case-class patterns (`*[Cc][Vv]*.pdf`, `[Rr]esume`) — matching is
  case-sensitive on Linux, which macOS hides.

### Fixed
- **The maintainer's real name was still in tracked test fixtures**, along with their
  actual CV filename, and a timezone example named their city. Replaced with neutral
  values; `tests/test_packaging.py` now fails on maintainer identity in any tracked file
  outside the authorship ones (a licence and a security contact must name a person).
- Removed a personal URL from a dashboard CSS comment.

### Upgrade

None required. Your existing `config/preferences.toml` is untouched on disk; it is simply
no longer tracked by git.

## [3.1.0] — 2026-08-18

### Added
- **Pull Jobs** — a button on the Overview that runs a full ingest + match pass on
  demand (`POST /ingest`), polling the run ledger for per-source progress. Until this,
  nothing in any interface could start a pass.
- **Filtered job cleanup** — `POST /jobs/purge` and a preview-then-confirm panel on the
  Jobs page. Presets for weak matches, dismissed jobs, stale postings, or everything
  matching the current filters. Applications, tailored CVs and triage notes are spared
  unconditionally.
- **Shared sign-in prompt** — `window.JA.signIn()`: any page's `401` raises a password
  modal and retries once, writing the same session token the Settings page uses.
- **Version reporting** — `/health` returns the running version; the dashboard sidebar
  shows it.

### Fixed
- **The triage queue under-reported by 5×.** `/jobs` reused the bot's shortlist builder,
  whose per-company cap is correct for a digest and wrong for a browsable queue: 231
  strong untriaged matches rendered as 46. Selection now runs through one shared
  predicate builder, so the count and the rows cannot diverge.
- `/jobs` defaulted to a 7-day window while the queue badge had no date filter, so a
  stale pipeline showed nothing at all. The default is now "any date".
- `/jobs` shipped the full posting `description` on every row — 95% of the payload, for
  text the list never renders. Responses dropped from 3.0 MB to 346 KB.

### Changed
- Dashboard dev port moved from 4321 to **1234** (Makefile, Astro config, CORS default,
  `.env.example`).
- Version is now read from a single literal in `src/jobagent/__init__.py`; `pyproject.toml`
  derives it. The FastAPI app previously reported `2.0` while the package said `3.0.0`.

## [3.0.0] — 2026-08-17

### Added
- **The agent harness.** `src/agentkit/` — a domain-agnostic, capability-aware multi-LLM
  layer: a ranked plan queue that doubles as the failover queue, nine degradation
  strategies, a circuit breaker, and a governed tool seam with no ungoverned path.
- **Baer, the assistant.** 14 in-process tools, permission tiers, argument-bound
  single-use confirmations, FTS5 retrieval over postings fenced as untrusted, and a
  fail-closed audit trail. Reachable from the CLI, the dashboard (page and floating
  bubble), and Telegram.
- **UI-editable profile.** Identity, CV, search preferences, sources and the ATS
  watchlist all persist to a gitignored `data/` overlay through a tabbed Settings page.
- Assistant eval harness with scored floors, and `llm_doctor` for offline diagnosis.

### Security
- Every non-GET API route requires a bearer token and fails closed without
  `DASHBOARD_PASSWORD`.

## [2.4.0] — 2026-07

### Added
- Job detail pages, on-demand fit checks, inline charts, location filters, pagination.

## [2.3.0] — 2026-07

### Added
- Application tracker and analytics: funnel, outcome rates, 30-day timeline.

## [2.2.0] — 2026-07

### Added
- Fit-checker — a confidence score with an explainable report.

## [2.1.0] — 2026-07

### Added
- Encrypted config UI (Fernet secret store), auth-gated settings API, custom
  OpenAI-compatible LLM provider.

## [2.0.0] — 2026-07

### Changed
- **FastAPI orchestrator became the sole backend.** The dashboard now calls a REST API
  instead of reading SQLite directly.

## [1.0.0] — 2026-06

### Added
- First working system: ingestion from six sources, heuristic + LLM matching, the
  Telegram bot, Tier-1 email applications, Tier-2 ATS form-fill, and VPS deployment
  units.

[Unreleased]: https://github.com/temesgen5335/personalAgent/compare/v3.7.0...HEAD
[3.7.0]: https://github.com/temesgen5335/personalAgent/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/temesgen5335/personalAgent/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/temesgen5335/personalAgent/compare/v3.4.1...v3.5.0
[3.4.1]: https://github.com/temesgen5335/personalAgent/compare/v3.4.0...v3.4.1
[3.4.0]: https://github.com/temesgen5335/personalAgent/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/temesgen5335/personalAgent/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/temesgen5335/personalAgent/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/temesgen5335/personalAgent/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/temesgen5335/personalAgent/compare/v1.0.0...v3.0.0
[2.4.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.4.0
[2.3.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.3.0
[2.2.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.2.0
[2.1.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.1.0
[2.0.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.0.0
[1.0.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v1.0.0
