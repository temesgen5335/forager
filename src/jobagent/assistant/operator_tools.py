"""Operator tools: what a coding agent (or the CLI operator) can do beyond asking.

These join the same governed toolbox Baer uses, but declare `surfaces={AGENT, CLI}` so
they never appear on chat — a Telegram turn must not pay for pull_jobs' schema
(memory.md: tool schemas are the dominant per-turn cost). Each follows the same rules as
the chat tools: call the service layer in-process, read store keys off the Store, fetch
wider than shown (R32), return text for the model and — where a coding agent benefits —
`ToolOutput(text, data)` with JSON beside it.

Nothing here sends, approves, submits, fills a form, writes a credential, writes the CV,
or deletes a posting. `draft_application` stops at `awaiting_approval` and hands the
decision back; there is no counterpart that sends it. The reachability test walks this
module's imports too (Task 9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agentkit.llm.types import ToolOutput, ToolSpec
from agentkit.permissions import Confirm, Permission, ToolPolicy
from agentkit.session import Surface
from jobagent.assistant.tools import FETCH_ROWS, MAX_ROWS, Registration, _rows, _schema

AGENT_SURFACES = frozenset({Surface.AGENT, Surface.CLI})


def _daemon_spawn(fn: Callable[[], None]) -> None:
    import threading
    threading.Thread(target=fn, daemon=True).start()


@dataclass
class OperatorDeps:
    """Injected so the operator tools are testable offline (R17): tests pass a synchronous
    `spawn`, a `FakeLLM` factory, and tmp paths so no real profile/CV/store is read."""

    db_path: str
    llm_factory: Callable[[], Any] | None = None            # default: build_llm(settings)
    spawn: Callable[[Callable[[], None]], None] = _daemon_spawn
    local_path: str | None = None                            # preferences .local.toml (tests pin)
    overlay_path: str | None = None                          # data/profile.json
    cv_loader: Callable[[], str] | None = None               # default: load_cv_master()
    env_path: str = ".env"

    def profile(self):
        from jobagent.preferences import load_preferences
        return load_preferences(local_path=self.local_path, overlay_path=self.overlay_path).profile

    def cv(self) -> str:
        if self.cv_loader is not None:
            return self.cv_loader()
        from jobagent.preferences import load_cv_master
        return load_cv_master()

    def llm(self, settings):
        if self.llm_factory is not None:
            return self.llm_factory()
        from jobagent.llm_client import build_llm
        return build_llm(settings)


def build_operator_tools(*, store, settings, deps: OperatorDeps, links) -> list[Registration]:
    ident = {"_required": True, "type": "string"}

    def setup_status(args: dict) -> str:
        from agentkit.llm.chain import build_chain

        prof = deps.profile()
        s = store.stats()
        h = store.pipeline_health()
        report = build_chain(settings, report=True)
        has_llm = bool(report.backends)
        cv_len = len(deps.cv() or "")
        # Store shape, not a value judgement: real vs demo vs empty.
        demo = sum(1 for j in store.get_jobs(limit=FETCH_ROWS)
                   if "[DEMO DATA" in (j.get("description") or ""))
        total = s.get("total_jobs") or 0
        state = "empty" if total == 0 else ("demo" if demo and demo >= total else "real")
        # The next command, so an agent is never left guessing.
        if not settings.dashboard_password:
            nxt = "make setup   # set a dashboard password and your profile"
        elif total == 0:
            nxt = "pull_jobs   # fetch and score (no credentials needed)"
        elif not (prof.target_roles and prof.core_skills):
            nxt = "apply_profile_change   # set your target_roles and core_skills, then rematch"
        else:
            nxt = "list_matches   # review the queue"
        lines = [
            f"store: {state} ({total} jobs, {s.get('matches') or 0} scored, "
            f"{s.get('queue') or 0} in the queue, {s.get('total_apps') or 0} applications)",
            f"last ingest: {h.get('last_ingest') or 'never'}",
            f"LLM: {'configured (' + str(len(report.backends)) + ' provider(s))' if has_llm else 'none — matching is heuristic-only'}",
            f"CV: {'present (' + str(cv_len) + ' chars)' if cv_len else 'missing — add it in Settings → CV'}",
            f"profile: {'personalised' if (prof.target_roles and prof.core_skills) else 'still generic — set target_roles and core_skills'}",
            f"dashboard password: {'set' if settings.dashboard_password else 'unset'}",
            f"next: {nxt}",
        ]
        data = {
            "jobs": total, "scored": s.get("matches") or 0, "queue": s.get("queue") or 0,
            "applications": s.get("total_apps") or 0, "store_state": state,
            "last_ingest": h.get("last_ingest"), "llm_configured": has_llm,
            "cv_present": bool(cv_len),
            "profile_personalised": bool(prof.target_roles and prof.core_skills),
            "next": nxt,
        }
        return ToolOutput("\n".join(lines), data=data)

    def current_profile(args: dict) -> str:
        p = deps.profile()
        data = {
            "target_roles": p.target_roles, "core_skills": p.core_skills,
            "seniority": p.seniority, "work_mode": p.work_mode, "location": p.location,
            "timezone": p.timezone, "remote_scope": p.remote_scope, "domains": p.domains,
            "must_haves": p.must_haves, "nice_to_haves": p.nice_to_haves,
            "exclude_keywords": p.exclude_keywords, "keywords": p.keywords,
            "preferred_locations": p.preferred_locations, "exclude_locations": p.exclude_locations,
            "skill_weights": p.skill_weights,
            "watchlist": {}, "sources": {},
        }
        from jobagent.preferences import load_preferences
        prefs = load_preferences(local_path=deps.local_path, overlay_path=deps.overlay_path)
        data["watchlist"] = {k: getattr(prefs.watchlist, k) for k in ("greenhouse", "lever", "ashby")}
        data["sources"] = {k: getattr(prefs.sources, k) for k in type(prefs.sources).model_fields}
        lines = [
            f"roles: {', '.join(p.target_roles) or '(none set)'}",
            f"skills: {', '.join(p.core_skills) or '(none set)'}",
            f"seniority: {p.seniority or '(unset)'}  work_mode: {p.work_mode or '(unset)'}  "
            f"remote_scope: {p.remote_scope}",
            f"location: {p.location or '(unset)'}  timezone: {p.timezone or '(unset)'}",
            f"watchlist: greenhouse={len(data['watchlist']['greenhouse'])} "
            f"lever={len(data['watchlist']['lever'])} ashby={len(data['watchlist']['ashby'])}",
            f"sources on: {', '.join(k for k, v in data['sources'].items() if v) or '(none)'}",
        ]
        return ToolOutput("\n".join(lines), data=data)

    def lifecycle(args: dict) -> str:
        from jobagent.core.schemas import ALLOWED_TRANSITIONS
        graph = {k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()}
        lines = [f"{state} → {', '.join(nexts) or '(terminal)'}" for state, nexts in graph.items()]
        return ToolOutput("\n".join(lines), data=graph)

    def list_matches(args: dict) -> str:
        sort = str(args.get("sort") or "score")
        limit = min(int(args.get("limit") or MAX_ROWS), MAX_ROWS)
        rows = store.get_matches(
            limit=FETCH_ROWS, offset=int(args.get("offset") or 0),
            min_score=float(args.get("min_score") or 0.6),
            location=str(args.get("location") or "any"),
            keywords=[w for w in str(args.get("q") or "").split() if w] or None,
            sources=[s.strip() for s in str(args.get("sources") or "").split(",") if s.strip()] or None,
            companies=[c.strip() for c in str(args.get("companies") or "").split(",") if c.strip()] or None,
            hide_triaged=not bool(args.get("include_triaged")),
        )
        keyfns = {
            "score": lambda m: -(m.get("score") or 0),
            "newest": lambda m: (m.get("first_seen_at") or ""),
            "salary": lambda m: -(m.get("salary_max") or m.get("salary_min") or 0),
        }
        rows = sorted(rows, key=keyfns.get(sort, keyfns["score"]),
                      reverse=(sort == "newest"))
        total = len(rows)
        text = _rows(rows[:limit], lambda m: (
            f"[{(m.get('id') or '')[:8]}] {float(m.get('score') or 0):.2f} "
            f"{m.get('title') or '?'} — {m.get('company') or 'unknown'} "
            f"({m.get('location') or 'n/a'}) via {m.get('source') or '?'}"), total=total)
        data = {"total": total, "sort": sort, "rows": [{
            "id": m.get("id"), "score": m.get("score"), "title": m.get("title"),
            "company": m.get("company"), "location": m.get("location"),
            "source": m.get("source"), "first_seen_at": m.get("first_seen_at"),
            "salary_min": m.get("salary_min"), "salary_max": m.get("salary_max"),
            "gaps": m.get("gaps"), "triage_state": m.get("triage_state"), "url": m.get("url"),
        } for m in rows[:limit]]}
        return ToolOutput(text, data=data)

    def company_dossier(args: dict) -> str:
        company = str(args.get("company", "")).strip()
        if not company:
            return "Name a company."
        rows = store.get_matches(limit=FETCH_ROWS, min_score=0.0,
                                 companies=[company], hide_triaged=False)
        apps = [a for a in store.list_applications(limit=FETCH_ROWS)
                if (a.get("company") or "").lower() == company.lower()]
        if not rows and not apps:
            return f"Nothing stored for {company!r}. Nothing was changed."
        gaps: dict[str, int] = {}
        for m in rows:
            for g in (m.get("gaps") or []):
                gaps[g] = gaps.get(g, 0) + 1
        top_gaps = sorted(gaps.items(), key=lambda kv: -kv[1])[:6]
        lines = [f"{company}: {len(rows)} stored posting(s), {len(apps)} application(s)"]
        lines += [f"  [{(m.get('id') or '')[:8]}] {float(m.get('score') or 0):.2f} "
                  f"{m.get('title') or '?'} ({m.get('triage_state') or 'active'})"
                  for m in rows[:MAX_ROWS]]
        if len(rows) > MAX_ROWS:
            lines.append(f"  …and {len(rows) - MAX_ROWS} more posting(s)")
        lines += [f"  application {a.get('status')}: {a.get('title') or '?'} "
                  f"({(a.get('created_at') or '')[:10]})" for a in apps[:MAX_ROWS]]
        if top_gaps:
            lines.append("recurring gaps: " + ", ".join(f"{g} ({n})" for g, n in top_gaps))
        data = {"company": company, "postings": len(rows), "applications": len(apps),
                "gaps": dict(top_gaps)}
        return ToolOutput("\n".join(lines), data=data)

    def fit_check(args: dict) -> str:
        from jobagent.fit import assess_fit

        job = store.get_job(str(args.get("job_id", "")))
        if not job:
            return "No posting with that id."
        cv = deps.cv()
        report = assess_fit(job, deps.profile(), cv, deps.llm(settings))
        return ToolOutput(report.format_short(), data=report.to_dict())

    def propose_profile_change(args: dict) -> str:
        from jobagent.assistant.profile_policy import ProfileRefused, preview_profile
        try:
            return preview_profile(str(args.get("field", "")), str(args.get("value", "")),
                                   local_path=deps.local_path, overlay_path=deps.overlay_path).render()
        except ProfileRefused as exc:
            return f"Refused: {exc}"

    def apply_profile_change(args: dict) -> str:
        from jobagent.assistant.profile_policy import ProfileRefused, apply_profile
        try:
            return apply_profile(str(args.get("field", "")), str(args.get("value", "")),
                                 overlay_path=deps.overlay_path, local_path=deps.local_path)
        except ProfileRefused as exc:
            return f"Refused: {exc}"

    def pull_jobs(args: dict) -> str:
        from jobagent.pipeline import LOCK_NAME, forage, new_run_id
        from jobagent.store import Store

        sources = [s.strip() for s in str(args.get("sources") or "").split(",") if s.strip()] or None
        if sources is not None:
            from jobagent.ingestion.gate import ALL_SOURCES
            unknown = [s for s in sources if s not in ALL_SOURCES]
            if unknown:
                return f"Refused: unknown source(s): {unknown}. Known: {ALL_SOURCES}"
        run_id = new_run_id()
        if not store.try_acquire_lock(LOCK_NAME, run_id):
            return "A forage is already running (locks expire after 2h). Poll recent_runs."
        profile = deps.profile()

        def forage_() -> None:
            own = Store(deps.db_path)          # its own thread, its own Store (R15)
            try:
                forage(own, settings, profile, llm=deps.llm(settings), run_id=run_id,
                       sources=sources, lock_held=True, trigger="agent")
            finally:
                own.close()

        deps.spawn(forage_)
        return ToolOutput(
            f"Started a forage {run_id}. It runs in the background; poll run_detail with "
            f"this id (or recent_runs) until a `run` event appears.", data={"run_id": run_id})

    def rematch(args: dict) -> str:
        from jobagent.matching import run_matching
        from jobagent.pipeline import new_run_id

        run_id = new_run_id()
        r = run_matching(store, deps.profile(), llm=deps.llm(settings), run_id=run_id)
        mode = "heuristic+LLM" if r.used_llm else "heuristic"
        return ToolOutput(f"Rescored {r.scored} posting(s) ({mode}); "
                          f"LLM-reranked {r.llm_reranked}.",
                          data={"scored": r.scored, "used_llm": r.used_llm,
                                "llm_reranked": r.llm_reranked, "run_id": run_id})

    def annotate_job(args: dict) -> str:
        job_id = str(args.get("job_id", ""))
        note = str(args.get("note", "")).strip()
        if not store.get_job(job_id):
            return "No posting with that id; nothing was changed."
        if not note:
            return "Give a note to save."
        store.set_triage(job_id, note=note)          # state and snooze left untouched (_KEEP)
        return f"Noted on {job_id[:8]}: {note}"

    def draft_application(args: dict) -> str:
        from jobagent.apply.prepare import prepare_application

        job = store.get_job(str(args.get("job_id", "")))
        if not job:
            return "No posting with that id."
        llm = deps.llm(settings)
        if llm is None:
            return "No LLM is configured — add a key in Settings → LLM to draft. Nothing was changed."
        cv = deps.cv()
        if not cv:
            return "No master CV — add it in Settings → CV. Nothing was changed."
        try:
            bundle = prepare_application(store, job, deps.profile(), cv, llm, settings=settings)
        except RuntimeError as exc:
            name = type(exc).__name__
            if "AllProvidersFailed" in name:
                return ("Every LLM provider is exhausted or rate-limited — try again later, "
                        "or run `make doctor PROBE=1`. Nothing was changed.")
            return f"Could not draft: {name}: {exc}. Nothing was changed."
        url = links("approve", bundle.application_id)
        ats = f" ATS check: {bundle.ats.coverage:.0%} coverage." if bundle.ats else ""
        text = (f"Drafted application {bundle.application_id[:8]} for "
                f"{job.get('title')} — {job.get('company') or 'unknown'} "
                f"(status: awaiting_approval).{ats}\n"
                f"Review and send it yourself: {url}\n"
                f"(I cannot approve or send anything — this is yours to do.)")
        data = {"application_id": bundle.application_id, "apply_method": bundle.apply_method,
                "cv_markdown": bundle.cv_markdown, "cover_letter": bundle.cover_letter,
                "email_subject": bundle.email_subject, "email_body": bundle.email_body,
                "ats_coverage": bundle.ats.coverage if bundle.ats else None, "review": bundle.review}
        return ToolOutput(text, data=data)

    def set_application_status(args: dict) -> str:
        from jobagent.lifecycle import IllegalTransition, NoSuchApplication, transition
        try:
            t = transition(store, str(args.get("application_id", "")), str(args.get("status", "")),
                           source="agent")
        except NoSuchApplication:
            return "No application with that id; nothing was changed."
        except IllegalTransition as exc:
            return (f"Refused: cannot move {exc.current} → {exc.target}. "
                    f"Allowed: {', '.join(exc.allowed) or 'none (terminal)'}. "
                    f"Use correct_application_status to override a genuine mistake.")
        return ToolOutput(f"Moved {t.application_id[:8]} {t.previous} → {t.status}.",
                          data={"status": t.status, "allowed_next": list(t.allowed_next)})

    def correct_application_status(args: dict) -> str:
        from jobagent.lifecycle import NoSuchApplication, transition
        reason = str(args.get("reason", "")).strip()
        if not reason:
            return "A correction needs a reason (it is audited)."
        try:
            t = transition(store, str(args.get("application_id", "")), str(args.get("status", "")),
                           correction=True, source="agent", reason=reason)
        except NoSuchApplication:
            return "No application with that id; nothing was changed."
        return ToolOutput(f"Corrected {t.application_id[:8]} → {t.status} ({reason}).",
                          data={"status": t.status, "corrected": t.corrected})

    R = Registration
    read = lambda n: ToolPolicy(n, Permission.READ, Confirm.NEVER)
    read_costly = lambda n: ToolPolicy(n, Permission.READ, Confirm.NEVER, costly=True)
    act = lambda n, d: ToolPolicy(n, Permission.ACT, Confirm.SESSION, describes=d)
    act_costly = lambda n, d: ToolPolicy(n, Permission.ACT, Confirm.SESSION, costly=True, describes=d)
    always = lambda n, d: ToolPolicy(n, Permission.ACT, Confirm.ALWAYS, describes=d)

    num = lambda d: {"type": "number", "description": d}
    intp = lambda d: {"type": "integer", "description": d}
    strp = lambda d: {"type": "string", "description": d}

    return [
        R(ToolSpec("setup_status", "What is configured, what is missing, and the next command to run.",
                   _schema()), setup_status, read("setup_status"), AGENT_SURFACES),
        R(ToolSpec("current_profile", "The search profile: roles, skills, weights, locations, watchlist, sources.",
                   _schema()), current_profile, read("current_profile"), AGENT_SURFACES),
        R(ToolSpec("lifecycle", "The allowed application status moves (the process graph).",
                   _schema()), lifecycle, read("lifecycle"), AGENT_SURFACES),
        R(ToolSpec("list_matches",
                   "Ranked matches with filters and a sort. Returns compact rows plus JSON.",
                   _schema(min_score=num("minimum score 0-1 (default 0.6)"),
                           sort={"type": "string", "enum": ["score", "newest", "salary"],
                                 "description": "order (default score)"},
                           location=strp("remote | hybrid | any"),
                           q=strp("keywords to match"), sources=strp("comma-separated source names"),
                           companies=strp("comma-separated company names"),
                           limit=intp(f"rows to show, max {MAX_ROWS}"),
                           offset=intp("rows to skip"),
                           include_triaged={"type": "boolean", "description": "include dismissed/snoozed"})),
          list_matches, read("list_matches"), AGENT_SURFACES),
        R(ToolSpec("company_dossier",
                   "Everything the store knows about one company: postings, applications, gaps.",
                   _schema(company={**ident, "description": "company name"})),
          company_dossier, read("company_dossier"), AGENT_SURFACES),
        R(ToolSpec("fit_check", "Assess one posting against your profile and CV (uses an LLM if configured).",
                   _schema(job_id={**ident, "description": "posting id"})),
          fit_check, read_costly("fit_check"), AGENT_SURFACES),
        R(ToolSpec("propose_profile_change", "What changing a search field would do. Changes nothing.",
                   _schema(field={**ident, "description": "profile field"},
                           value={**ident, "description": "new value (comma-separated for lists)"})),
          propose_profile_change, read("propose_profile_change"), AGENT_SURFACES),
        R(ToolSpec("pull_jobs", "Start an ingest → match pass in the background; returns a run id to poll.",
                   _schema(sources=strp("comma-separated source names; omit for all enabled"))),
          pull_jobs, act_costly("pull_jobs", "Fetch and score new postings"), AGENT_SURFACES),
        R(ToolSpec("rematch", "Re-score every stored posting against the current profile.",
                   _schema()), rematch, act_costly("rematch", "Re-score stored postings"), AGENT_SURFACES),
        R(ToolSpec("annotate_job", "Attach a note to a posting without changing its triage state.",
                   _schema(job_id={**ident, "description": "posting id"},
                           note={**ident, "description": "the note"})),
          annotate_job, act("annotate_job", "Save a note on a posting"), AGENT_SURFACES),
        R(ToolSpec("draft_application",
                   "Tailor a CV, cover letter and email for one posting and save them for your "
                   "review. Sends nothing — you approve and send yourself.",
                   _schema(job_id={**ident, "description": "posting id"})),
          draft_application, act_costly("draft_application", "Draft application assets (not sent)"),
          AGENT_SURFACES),
        R(ToolSpec("set_application_status", "Move an application along its lifecycle (legal moves only).",
                   _schema(application_id={**ident, "description": "application id"},
                           status={**ident, "description": "new status"})),
          set_application_status, always("set_application_status", "Change an application's status"),
          AGENT_SURFACES),
        R(ToolSpec("correct_application_status",
                   "Override the lifecycle to fix a mistake. Audited with your reason.",
                   _schema(application_id={**ident, "description": "application id"},
                           status={**ident, "description": "corrected status"},
                           reason={**ident, "description": "why (recorded)"})),
          correct_application_status,
          always("correct_application_status", "Force an out-of-order status (audited)"), AGENT_SURFACES),
        R(ToolSpec("apply_profile_change", "Change a search field. Identity and the CV are not writable.",
                   _schema(field={**ident, "description": "profile field"},
                           value={**ident, "description": "new value (comma-separated for lists)"})),
          apply_profile_change, always("apply_profile_change", "Change a search-profile field"),
          AGENT_SURFACES),
    ]
