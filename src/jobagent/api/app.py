"""FastAPI orchestrator — the single backend the Telegram bot and Astro dashboard
both call (v2). Wraps the existing service layer (store, matching, apply, ats, llm).

SQLite is single-thread and FastAPI runs sync handlers in a threadpool, so every
handler opens its OWN Store and closes it. The app is created via create_app() so
tests can inject a temp store, a fake LLM, and a fake mailer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request

from jobagent.api.ratelimit import RateLimiter, client_key
from pydantic import BaseModel

from jobagent import __version__
from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.generators import draft_followup
from jobagent.apply.ats import apply_target
from jobagent.apply.ats_flow import create_ats_application, run_ats
from jobagent.apply.email_send import send_email
from jobagent.bot.service import MatchFilter, ranked_matches
from jobagent.config import get_settings, reload_settings
from jobagent.core.schemas import Event, allowed_next
from jobagent.fit import assess_fit
from jobagent.ingestion.gate import ALL_SOURCES, resolve_sources
from jobagent.lifecycle import IllegalTransition, NoSuchApplication, VALID_STATUSES, transition
from jobagent.llm_client import AllProvidersFailed, build_llm
from jobagent.matching import run_matching
from jobagent.pipeline import forage
from jobagent.preferences import (
    Profile,
    Sources,
    Watchlist,
    load_cv_master,
    load_preferences,
    save_cv_master,
    save_overlay,
)
from jobagent.secrets_store import MANAGED_FIELDS, SecretStore, masked_view
from jobagent.store import Store
from jobagent.upskill import upskill_report

_UNSET = object()

# `LLMService.complete` raises AllProvidersFailed when every provider fails, and the
# common cause is a free-tier daily limit — an expected, self-healing condition. Left
# unhandled it surfaced as a 500 Internal Server Error, which tells the operator
# nothing and reads like a code fault. Found by exercising the running system with all
# three free tiers exhausted.


def _llm_unavailable(exc: Exception) -> HTTPException:
    """Turn provider exhaustion into a 503 the caller can act on."""
    detail = str(exc)
    hint = ""
    if "rate_limit" in detail or "429" in detail or "quota" in detail.lower():
        hint = (" Every configured provider is rate-limited or out of quota — "
                "this usually clears on its own. Run `make doctor PROBE=1` to see which.")
    return HTTPException(503, f"No LLM provider could serve that request.{hint}")


# Stripped from the /jobs LIST only — the store keeps both forever, and /job/{id}
# still serves the description because the detail page renders it. `raw` is the
# untouched source payload nothing reads. `description` is the full posting text:
# measured at 95% of the list payload (136 KB of 143 KB over 20 rows) while the
# dashboard's MatchRow does not even declare it. A storage rule is not a transport rule.
_WIRE_OMIT = ("raw", "description")


def _strip_heavy(rows: list[dict]) -> list[dict]:
    for r in rows:
        for field in _WIRE_OMIT:
            r.pop(field, None)
    return rows


def _decode_gaps(rows: list[dict]) -> list[dict]:
    """gaps is stored as JSON text; hand clients a real array so neither the
    dashboard nor any other consumer has to parse SQLite's encoding."""
    for r in rows:
        raw = r.get("gaps")
        if isinstance(raw, str):
            try:
                r["gaps"] = json.loads(raw or "[]")
            except (ValueError, TypeError):
                r["gaps"] = []
        elif raw is None:
            r["gaps"] = []
    return rows


class JobIdReq(BaseModel):
    job_id: str


class LoginReq(BaseModel):
    password: str


class ConfigPatch(BaseModel):
    values: dict


class ProfilePatch(BaseModel):
    """A partial profile update. Every section is optional so the UI can save one tab
    without touching the others; a section left None is not modified, and the CV is
    a separate large field kept out of the JSON overlay."""

    profile: dict | None = None
    watchlist: dict | None = None
    sources: dict | None = None
    cv_master: str | None = None


class FollowupReq(BaseModel):
    days_waiting: int | None = None


class TriageReq(BaseModel):
    action: str                     # dismiss | snooze | note | clear
    days: int = 3                   # snooze horizon
    note: str | None = None


class PurgeReq(BaseModel):
    """Filtered cleanup of stored postings. Mirrors the /jobs query parameters, so the
    rows a user is looking at are the rows this deletes.

    `dry_run` defaults to **True** on purpose: a request that omits the field previews.
    The destructive reading has to be typed out, never fallen into.
    """
    dry_run: bool = True
    # Same names /jobs uses, so the dashboard can hand its filter state straight over.
    days: int = 0                       # posted/first-seen within N days
    location: str = "any"
    q: str | None = None
    exclude: str | None = None
    include: str | None = None
    sources: str | None = None
    companies: str | None = None
    # Cleanup-specific selectors.
    below_score: float | None = None    # delete rows scoring UNDER this
    min_score: float = 0.0
    last_seen_days: int | None = None   # not seen in a feed for N days
    triage_states: str | None = None    # dismissed | snoozed | untriaged (comma list)
    include_unscored: bool = False      # jobs with no matches row at all
    vacuum: bool = False                # reclaim file space afterwards (rewrites the db)


class ProposalDecision(BaseModel):
    action: str            # accept | dismiss


class StatusReq(BaseModel):
    status: str
    # Escape hatch for fixing a mis-click. Bypasses the transition map and logs an
    # event, so an out-of-order change is possible but never silent.
    correction: bool = False


def _token_for(password: str, master_key: str) -> str:
    return hashlib.sha256(f"{password}|{master_key}".encode()).hexdigest()


def _ingest_task(db_path: str, settings, profile, llm, run_id: str) -> None:
    """The background half of POST /ingest. The endpoint acquired the lock under this
    run_id before scheduling us; `forage` releases it."""
    store = Store(db_path)
    try:
        forage(store, settings, profile, llm=llm, run_id=run_id, lock_held=True,
               trigger="api")
    finally:
        store.close()


def create_app(settings=None, profile=None, llm: Any = _UNSET, cv_master: str | None = None, mailer=None) -> FastAPI:
    settings = settings or get_settings()
    mailer = mailer or send_email
    # Injected llm (tests) is fixed; otherwise build fresh per call so config edits apply.
    llm_injected = llm is not _UNSET
    # Profile and CV are injected in tests; in production they are loaded FRESH per
    # request (see _profile()/_cv_master() below), so an edit in Settings → Profile
    # takes effect immediately, the same way config edits do — never captured once at
    # startup.
    profile_injected = profile is not None
    cv_injected = cv_master is not None

    if settings.require_auth_reads and not settings.dashboard_password:
        # Refuse to start rather than serve something unusable. With reads gated and no
        # password there is no token that works, so every page — including the
        # dashboard's own SSR fetches — would 403 forever, which reads as "the app is
        # broken" rather than "you missed a setting".
        raise RuntimeError(
            "JOBAGENT_REQUIRE_AUTH_READS is on but DASHBOARD_PASSWORD is unset — "
            "nothing would be readable. Set a password, or turn read-auth off.")

    app = FastAPI(title="Personal Job Agent API", version=__version__)

    # The dashboard runs on a different origin and calls the API from the browser.
    from fastapi.middleware.cors import CORSMiddleware

    origins = [o.strip() for o in (settings.cors_origins or "*").split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware, allow_origins=origins,
        allow_methods=["*"], allow_headers=["*"],
    )

    def store() -> Store:
        return Store(settings.db_path)

    def _llm():
        return llm if llm_injected else build_llm(get_settings())

    def _profile():
        return profile if profile_injected else load_preferences().profile

    def _cv_master() -> str:
        return cv_master if cv_injected else load_cv_master()

    # --- auth -----------------------------------------------------------------
    # Gates EVERY state-changing or cost-incurring route, not just /config. These
    # endpoints can send email as you, submit ATS forms, and spend LLM quota, so an
    # unauthenticated caller who can reach the port must not be able to drive them.
    # GETs stay open: they are read-only and the dashboard renders them server-side
    # without a token. tests/test_api.py asserts this gate covers every non-GET route.
    def _expected_token() -> str | None:
        return _token_for(settings.dashboard_password, settings.master_key) if settings.dashboard_password else None

    def require_auth(authorization: str | None = Header(None)) -> None:
        expected = _expected_token()
        if expected is None:
            # Fail closed: with no password there is no way to authenticate, so
            # refuse outright rather than leaving writes open.
            raise HTTPException(403, "Writes disabled — set DASHBOARD_PASSWORD to enable authenticated access.")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != expected:
            raise HTTPException(401, "Unauthorized.")

    auth = [Depends(require_auth)]

    def require_read_auth(authorization: str | None = Header(None)) -> None:
        """Gate GET routes when JOBAGENT_REQUIRE_AUTH_READS is on.

        Off by default, which is right on the default 127.0.0.1 bind. Turn it on for
        any deployment the network can reach: `/applications` and `/followups` reveal
        where you applied, what was rejected, and where you are interviewing.

        Unlike `require_auth` this does NOT fail closed without a password — with the
        flag off it is a no-op, and with the flag on but no password configured there
        would be no way to read anything at all, including the dashboard's own SSR
        fetches. Refusing to start is better than that, so `create_app` checks the
        combination up front.
        """
        if not settings.require_auth_reads:
            return
        require_auth(authorization)

    read_auth = [Depends(require_read_auth)]

    # Bounded per client, per class. See jobagent/api/ratelimit.py for why this is
    # in-process rather than shared.
    _limiters = {
        "assistant": RateLimiter(settings.rate_limit_assistant),
        "ingest": RateLimiter(settings.rate_limit_ingest),
        "write": RateLimiter(settings.rate_limit_write),
    }
    app.state.limiters = _limiters      # exposed so tests can reset between cases

    def _limit(kind: str):
        def dependency(request: Request) -> None:
            if not settings.rate_limit_enabled:
                return
            allowed, retry_after = _limiters[kind].allow(client_key(request))
            if not allowed:
                # 429 with Retry-After, because a caller that cannot tell "slow down"
                # from "broken" will simply retry harder.
                raise HTTPException(
                    429, f"Rate limit reached for {kind} requests. Try again in "
                         f"{int(retry_after) + 1}s, or raise "
                         f"JOBAGENT_RATE_LIMIT_{kind.upper()}.",
                    headers={"Retry-After": str(int(retry_after) + 1)})
        return Depends(dependency)

    write_limit = [_limit("write")]
    ingest_limit = [_limit("ingest")]
    assistant_limit = [_limit("assistant")]

    @app.post("/auth/login")
    def login(body: LoginReq):
        if not settings.dashboard_password:
            raise HTTPException(403, "Config UI disabled — set DASHBOARD_PASSWORD.")
        if body.password != settings.dashboard_password:
            raise HTTPException(401, "Wrong password.")
        return {"token": _token_for(body.password, settings.master_key)}

    def _effective_managed() -> dict:
        # env baseline (create_app's settings) overlaid by the encrypted store.
        base = {f: getattr(settings, f, None) for f in MANAGED_FIELDS}
        try:
            base.update({k: v for k, v in SecretStore().load().items() if k in MANAGED_FIELDS})
        except Exception:  # noqa: BLE001 — unreadable store → show env baseline only
            pass
        return base

    @app.get("/config", dependencies=auth)
    def get_config():
        return {"config": masked_view(_effective_managed())}

    @app.put("/config", dependencies=auth)
    def put_config(patch: ConfigPatch):
        try:
            SecretStore().update(patch.values)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        reload_settings()   # so other endpoints (build_llm) pick up new keys this process
        return {"config": masked_view(_effective_managed())}

    # --- profile: identity, background, search preferences, CV ---------------------
    # Auth-gated on BOTH verbs: unlike /config's non-secret read, the profile carries
    # personal data (name, email, phone, CV), so even reading it requires the token.
    # Persisted to the gitignored data/ overlay, never to a committed file (R22).

    @app.get("/profile", dependencies=auth)
    def get_profile():
        prefs = load_preferences()
        return {
            "profile": prefs.profile.model_dump(),
            "watchlist": prefs.watchlist.model_dump(),
            "sources": prefs.sources.model_dump(),
            # Presence + size, not the text — the CV can be large and this is a summary.
            "cv": {"present": bool(_cv_master()), "chars": len(_cv_master())},
        }

    @app.put("/profile", dependencies=auth)
    def put_profile(patch: ProfilePatch):
        # Validate each supplied section against its model so a bad field is a 422 here
        # rather than a surprise the next time matching runs. Unknown profile keys are
        # allowed (Profile has extra="allow") but the known ones must type-check.
        # exclude_unset is the load-bearing detail: it persists ONLY the keys the
        # client actually sent. Without it, model_dump() emits every field's default,
        # so saving one tab writes name="", email="" … over the whole profile section —
        # shadowing the real values from the lower layers. (Caught in a browser: saving
        # the Search tab blanked the identity fields.) Section-wise merge in
        # save_overlay then leaves untouched fields exactly as they were.
        overlay: dict = {}
        try:
            if patch.profile is not None:
                overlay["profile"] = Profile(**patch.profile).model_dump(exclude_unset=True)
            if patch.watchlist is not None:
                overlay["watchlist"] = Watchlist(**patch.watchlist).model_dump(exclude_unset=True)
            if patch.sources is not None:
                overlay["sources"] = Sources(**patch.sources).model_dump(exclude_unset=True)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"Invalid profile data: {exc}")

        if patch.cv_master is not None:
            save_cv_master(patch.cv_master)
        if overlay:
            save_overlay(overlay)

        prefs = load_preferences()
        return {
            "profile": prefs.profile.model_dump(),
            "watchlist": prefs.watchlist.model_dump(),
            "sources": prefs.sources.model_dump(),
            "cv": {"present": bool(_cv_master()), "chars": len(_cv_master())},
        }

    @app.get("/profile/cv", dependencies=auth)
    def get_cv():
        """The full CV text, for the editor. Auth-gated — it is personal data."""
        return {"cv_master": _cv_master()}

    @app.get("/health")
    def health():
        chain = _llm()
        return {
            "status": "ok",
            "version": __version__,
            "store_exists": Path(settings.db_path).exists(),
            "llm_chain": chain.chain if chain else [],
            "config_ui": bool(settings.dashboard_password),
        }

    @app.get("/stats", dependencies=read_auth)
    def stats():
        s = store()
        try:
            return s.stats()
        finally:
            s.close()

    @app.get("/jobs", dependencies=read_auth)
    def jobs(days: int = 0, location: str = "any", q: str | None = None,
             exclude: str | None = None, include: str | None = None,
             sources: str | None = None, limit: int = 50, offset: int = 0,
             min_salary: float | None = None):
        split = lambda v: [x.strip() for x in (v or "").split(",") if x.strip()]  # noqa: E731
        flt = MatchFilter(
            max_age_days=days or None, location=location,
            keywords=[w for w in (q or "").replace(",", " ").split() if w],
            exclude_locations=split(exclude), include_locations=split(include),
            sources=split(sources),
            # Annualised comparison, and unknown pay is kept — see _row_predicates.
            min_salary=min_salary,
            # The dashboard renders dismissed/snoozed rows with an Undo control, so
            # unlike every other consumer it wants them in the result set.
            hide_triaged=False,
        )
        s = store()
        try:
            # No per-company cap: this route feeds a browsable stash list, not a
            # shortlist. Capping here made the dashboard queue disagree with the
            # queue count in stats() — the number you are asked to clear must be
            # the number of rows you are given. The bot keeps its cap.
            return {"jobs": _strip_heavy(_decode_gaps(
                ranked_matches(s, limit, flt, offset=offset, max_per_company=None)))}
        finally:
            s.close()

    @app.get("/applications", dependencies=read_auth)
    def applications(limit: int = 200):
        s = store()
        try:
            rows = s.list_applications(limit)
        finally:
            s.close()
        # allowed_next travels with each row so the UI never duplicates the
        # transition map — the Python definition stays the single source of truth.
        for r in rows:
            r["allowed_next"] = sorted(allowed_next(r["status"]))
        return {"applications": rows}

    @app.get("/job/{job_id}", dependencies=read_auth)
    def job_detail(job_id: str):
        s = store()
        try:
            job = s.get_job(job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            match = s.get_match(job_id) or {}
        finally:
            s.close()
        return _decode_gaps([{**job, **match}])[0]

    @app.patch("/applications/{app_id}", dependencies=auth)
    def update_application(app_id: str, body: StatusReq):
        if body.status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. One of: {sorted(VALID_STATUSES)}")
        s = store()
        try:
            try:
                t = transition(s, app_id, body.status, correction=body.correction, source="api")
            except NoSuchApplication:
                raise HTTPException(404, "Application not found.") from None
            except IllegalTransition as exc:
                # 422: the value is a real status, but the move is not part of the
                # process. Name the legal moves so the caller can act on it.
                raise HTTPException(422, {
                    "message": f"Cannot move {exc.current} → {exc.target}.",
                    "current": exc.current,
                    "allowed": exc.allowed,
                    "hint": "Pass correction=true to override a mis-click (audited).",
                }) from None
        finally:
            s.close()
        return {"id": app_id, "status": t.status, "allowed_next": list(t.allowed_next)}

    @app.post("/triage/{job_id}", dependencies=auth + write_limit)
    def triage(job_id: str, body: TriageReq):
        """One decision per job: dismiss (hide from the queue), snooze (hide for N
        days, lapses back on its own), note (annotate, stays live), clear (undo)."""
        from datetime import datetime, timedelta, timezone

        s = store()
        try:
            if not s.get_job(job_id):
                raise HTTPException(404, "Job not found.")
            if body.action == "dismiss":
                row = s.set_triage(job_id, state="dismissed", snoozed_until=None)
            elif body.action == "snooze":
                until = (datetime.now(timezone.utc) + timedelta(days=max(1, body.days))).isoformat()
                row = s.set_triage(job_id, state="snoozed", snoozed_until=until)
            elif body.action == "note":
                row = s.set_triage(job_id, note=body.note or "")
            elif body.action == "clear":
                s.clear_triage(job_id)
                row = s.get_triage(job_id) or {"job_id": job_id, "state": None, "note": None}
            else:
                raise HTTPException(400, "action must be dismiss | snooze | note | clear")
        finally:
            s.close()
        return {"job_id": job_id, "state": row.get("state"),
                "snoozed_until": row.get("snoozed_until"), "note": row.get("note")}

    @app.post("/jobs/purge", dependencies=auth + write_limit)
    def purge_jobs(body: PurgeReq):
        """Delete stored postings matching the same filters /jobs lists by.

        Preview and apply are the SAME query — only `dry_run` differs — so what a user
        approves is what gets deleted. Anything with an application, a tailored CV, or a
        stash note is spared whatever the filters say.
        """
        split = lambda v: [x.strip() for x in (v or "").split(",") if x.strip()]  # noqa: E731
        run_id = uuid.uuid4().hex[:12]
        s = store()
        try:
            result = s.purge_jobs(
                dry_run=body.dry_run,
                run_id=run_id,
                vacuum=body.vacuum and not body.dry_run,
                include_unscored=body.include_unscored,
                min_score=body.min_score,
                max_score=body.below_score,
                max_age_days=body.days or None,
                last_seen_days=body.last_seen_days,
                location=body.location,
                keywords=[w for w in (body.q or "").replace(",", " ").split() if w],
                exclude_locations=split(body.exclude),
                include_locations=split(body.include),
                sources=split(body.sources),
                companies=split(body.companies),
                triage_states=split(body.triage_states),
            )
            # Recomputed after the delete so the caller can refresh counts without a
            # second round trip — the nav badge and every stat move under a purge.
            result["stats"] = s.stats()
        finally:
            s.close()
        if result.get("unfiltered"):
            # No predicate at all would mean "the whole store". Refuse rather than
            # guess: an empty filter set is a caller bug far more often than an intent.
            raise HTTPException(400, "Refusing an unfiltered purge — narrow it with at "
                                     "least one filter (score, date, source, or stash).")
        result["run_id"] = run_id
        return result

    @app.post("/demo/clear", dependencies=auth)
    def demo_clear():
        """Remove the seeded demo data. The 'go live' path is this call followed by
        POST /ingest (a real, keyless pull). Auth-gated (R19)."""
        s = store()
        try:
            cleared = s.clear_demo()
            return {"cleared": cleared, "stats": s.stats()}
        finally:
            s.close()

    @app.get("/inbox/proposals", dependencies=read_auth)
    def inbox_proposals(state: str = "pending", limit: int = 50):
        """Detected outcomes awaiting a decision. Read-only: nothing here has been
        applied, and nothing will be until the operator accepts it."""
        s = store()
        try:
            rows = s.list_proposals(state=state, limit=limit)
        finally:
            s.close()
        # allowed_next travels with each row so the UI never has to guess whether the
        # proposal is even a legal move from the application's current status.
        for r in rows:
            r["allowed_next"] = sorted(allowed_next(r.get("current_status", "")))
            r["is_legal"] = r["proposed"] in r["allowed_next"]
        return {"proposals": rows}

    @app.post("/inbox/proposals/{proposal_id}", dependencies=auth + write_limit)
    def decide_proposal(proposal_id: str, body: ProposalDecision):
        """Accept or dismiss a detected outcome.

        Accepting applies the SAME transition rules a manual edit obeys — the detector
        gets no privileged path into the lifecycle. An illegal move is refused with the
        legal ones named, exactly as PATCH /applications does, rather than silently
        corrected: a mis-detected outcome must not be able to rewrite history just
        because it arrived by email.
        """
        s = store()
        try:
            proposal = s.get_proposal(proposal_id)
            if not proposal:
                raise HTTPException(404, "No such proposal.")
            if proposal["state"] != "pending":
                raise HTTPException(409, f"Already {proposal['state']}.")

            if body.action == "dismiss":
                s.set_proposal_state(proposal_id, "dismissed")
                return {"id": proposal_id, "state": "dismissed"}
            if body.action != "accept":
                raise HTTPException(400, "action must be accept | dismiss")

            try:
                t = transition(s, proposal["application_id"], proposal["proposed"],
                               source="inbox")
            except NoSuchApplication:
                raise HTTPException(404, "Application not found.") from None
            except IllegalTransition as exc:
                raise HTTPException(422, {
                    "message": f"Cannot move {exc.current} → {exc.target}.",
                    "current": exc.current,
                    "allowed": exc.allowed,
                    "hint": "Dismiss this proposal, or change the status by hand.",
                }) from None
            s.set_proposal_state(proposal_id, "accepted")
            # Audited: an outcome that entered the record from an email should be
            # distinguishable from one the operator typed, forever.
            s.log_event(Event(kind="outcome_accepted", job_id=t.job_id,
                              payload={"application_id": proposal["application_id"],
                                       "from": t.previous, "to": t.status,
                                       "source": "inbox", "proposal_id": proposal_id,
                                       "message_id": proposal["message_id"]}))
        finally:
            s.close()
        return {"id": proposal_id, "state": "accepted", "status": t.status}

    @app.get("/followups", dependencies=read_auth)
    def followups(after_days: int = 7):
        """Submitted applications that have gone quiet. Read-only."""
        s = store()
        try:
            return {"followups": s.applications_needing_followup(after_days=after_days),
                    "after_days": after_days}
        finally:
            s.close()

    @app.get("/upskill", dependencies=read_auth)
    def upskill(min_score: float = 0.5, limit: int = 20):
        """Recurring skill gaps across scored matches, weighted by fit. Read-only, no
        model — the LLM learning plan lives in `scripts/upskill.py`."""
        s = store()
        try:
            return upskill_report(s, min_score=min_score, limit=limit)
        finally:
            s.close()

    @app.post("/followups/{app_id}/draft", dependencies=auth)
    def followup_draft(app_id: str, body: FollowupReq | None = None):
        """Draft a nudge for a quiet application.

        DRAFT ONLY — there is deliberately no send endpoint for follow-ups. The user
        sends these personally, so nothing here can put mail on the wire.
        """
        current_llm = _llm()
        if current_llm is None:
            raise HTTPException(400, "No LLM configured (set an LLM key).")
        s = store()
        try:
            application = s.get_application(app_id)
            if not application:
                raise HTTPException(404, "Application not found.")
            job = s.get_job(application["job_id"])
            if not job:
                raise HTTPException(404, "Job not found.")
            days = (body.days_waiting if body and body.days_waiting is not None else 7)
            try:
                subject, text = draft_followup(_profile().name or "", job, days, current_llm)
            except RuntimeError as exc:
                if isinstance(exc, AllProvidersFailed):
                    raise _llm_unavailable(exc) from exc
                raise
            # Logged so the reminder stops firing until the next window.
            s.log_event(Event(kind="followup_drafted", job_id=application["job_id"],
                              payload={"application_id": app_id, "days_waiting": days}))
        finally:
            s.close()
        return {"application_id": app_id, "subject": subject, "body": text,
                "to": job.get("apply_email"), "sent": False}

    @app.get("/analytics", dependencies=read_auth)
    def analytics():
        s = store()
        try:
            return s.application_analytics()
        finally:
            s.close()

    @app.post("/match", dependencies=auth + assistant_limit)
    def match():
        s = store()
        try:
            # No wrapper here: llm_score swallows provider failures and matching
            # falls back to heuristic scoring, so this path degrades rather than
            # failing. Asserted in tests/test_api.py so nobody "fixes" it into a 503.
            r = run_matching(s, _profile(), llm=_llm())
            return {"scored": r.scored, "used_llm": r.used_llm, "llm_reranked": r.llm_reranked}
        finally:
            s.close()

    @app.post("/ingest", status_code=202, dependencies=auth + ingest_limit)
    def ingest(bg: BackgroundTasks):
        # The id is returned immediately so the caller can watch /runs/{id} while
        # the background task is still going.
        run_id = uuid.uuid4().hex[:12]
        s = store()
        try:
            # Same lock a forage takes (M5) — acquired HERE, not in the task, so
            # the caller learns synchronously that a forage is already running.
            if not s.try_acquire_lock("pipeline", run_id):
                raise HTTPException(409, "An ingestion forage is already running.")
        finally:
            s.close()
        bg.add_task(_ingest_task, settings.db_path, get_settings(), _profile(), _llm(), run_id)
        return {"status": "started", "run_id": run_id}

    @app.get("/sources", dependencies=read_auth)
    def sources_view():
        """Selectable ingest sources, which are enabled, and what is actually in the
        store — the dashboard needs all three: the full set for the Settings picker,
        the enabled set to preselect it, and the stored set for the Jobs visibility
        filter (offering a source with zero stored jobs is just noise)."""
        s = store()
        try:
            in_store = s.stats()["by_source"]
        finally:
            s.close()
        return {
            "available": ALL_SOURCES,
            "enabled": sorted(resolve_sources(get_settings(), load_preferences().sources)),
            "in_store": in_store,
        }

    @app.get("/runs", dependencies=read_auth)
    def runs(limit: int = 20):
        s = store()
        try:
            return {"runs": s.list_runs(limit)}
        finally:
            s.close()

    @app.get("/runs/{run_id}", dependencies=read_auth)
    def run_events(run_id: str):
        s = store()
        try:
            events = s.events_for_run(run_id)
        finally:
            s.close()
        if not events:
            raise HTTPException(404, "No events for that run id.")
        return {"run_id": run_id, "events": events}

    @app.post("/fit", dependencies=auth + assistant_limit)
    def fit(req: JobIdReq):
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
        finally:
            s.close()
        try:
            return assess_fit(job, _profile(), _cv_master(), _llm()).to_dict()
        except RuntimeError as exc:
            if isinstance(exc, AllProvidersFailed):
                raise _llm_unavailable(exc) from exc
            raise

    @app.post("/apply/prepare", dependencies=auth + assistant_limit)
    def apply_prepare(req: JobIdReq):
        current_llm = _llm()
        if current_llm is None:
            raise HTTPException(400, "No LLM configured (set an LLM key).")
        cv_master = _cv_master()
        if not cv_master:
            raise HTTPException(400, "No master CV — add it in Settings → Profile.")
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            try:
                b = prepare_application(s, job, _profile(), cv_master, current_llm, settings=settings)
            except RuntimeError as exc:
                if isinstance(exc, AllProvidersFailed):
                    raise _llm_unavailable(exc) from exc
                raise
            return {
                "application_id": b.application_id, "apply_method": b.apply_method,
                "cv_markdown": b.cv_markdown, "cover_letter": b.cover_letter,
                "email_subject": b.email_subject, "email_body": b.email_body,
                "ats": b.ats.as_dict() if b.ats else None,
                "review": b.review,
            }
        finally:
            s.close()

    @app.post("/apply/{app_id}/approve", dependencies=auth)
    def apply_approve(app_id: str):
        s = store()
        try:
            return {"result": approve_and_send(s, app_id, settings, _profile(), mailer=mailer)}
        finally:
            s.close()

    @app.post("/ats/preview", dependencies=auth)
    def ats_preview(req: JobIdReq):
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            if apply_target(job)[0] is None:
                raise HTTPException(400, "Not a supported ATS (Greenhouse/Lever/Ashby).")
            app_id = create_ats_application(s, job)
            Path("artifacts").mkdir(exist_ok=True)
            shot = f"artifacts/ats_{app_id}.png"
            res = run_ats(s, app_id, _profile(), shot, submit=False)
        finally:
            s.close()
        return _ats_response(app_id, res)

    @app.post("/ats/{app_id}/submit", dependencies=auth)
    def ats_submit(app_id: str):
        s = store()
        try:
            Path("artifacts").mkdir(exist_ok=True)
            shot = f"artifacts/ats_{app_id}_submit.png"
            res = run_ats(s, app_id, _profile(), shot, submit=True)
        finally:
            s.close()
        return _ats_response(app_id, res)

    # The assistant lives in its own module: it is the only surface with a two-phase
    # confirmation flow, and keeping that out of here stops it being mistaken for the
    # ordinary single-request pattern every other route follows.
    from jobagent.api.assistant_routes import register as register_assistant

    # Held on app.state so the pending-approval registry is reachable — tests drive the
    # two-phase flow through it, and an operator surface can list what is waiting.
    app.state.assistant_pending = register_assistant(
        app, store_factory=store, settings_factory=get_settings, auth=auth,
        read_auth=read_auth, limit=assistant_limit)

    return app


def _ats_response(app_id: str, res) -> dict:
    return {
        "application_id": app_id, "platform": res.platform, "url": res.url,
        "filled": res.filled, "missing": res.missing,
        "captcha_detected": res.captcha_detected, "submitted": res.submitted,
        "screenshot_path": res.screenshot_path, "summary": res.summary(),
    }
