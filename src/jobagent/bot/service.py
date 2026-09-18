"""Pure bot logic — no telegram import, so it's unit-testable on its own.
The telegram wiring in app.py and the notifier in notify.py call into this.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobagent.assistant import ASSISTANT_NAME
from jobagent.digest import diversify, format_matches
from jobagent.store import Store
from jobagent.upskill import upskill_report

HELP_TEXT = (
    "🤖 *Personal Job Agent*\n\n"
    "/menu — interactive menu (filters, jobs, apply)\n"
    "/jobs [N] — top N job matches (default 10)\n"
    "/apply <rank> — draft a tailored application for job #rank\n"
    "/status — pipeline stats (jobs, senses, matches)\n"
    "/upskill [fit] — recurring skill gaps across your matches (default fit ≥ 0.5)\n"
    f"/ask <question> — ask {ASSISTANT_NAME} about your pipeline "
    f"(or just say “{ASSISTANT_NAME}, …”)\n"
    "/help — this message\n\n"
    "Daily digests arrive automatically."
)

# Date filter presets shown in the menu: (label, days). 0 = any time.
DATE_PRESETS = [("Today", 1), ("2 days", 2), ("Week", 7), ("Month", 30), ("Any", 0)]
LOCATIONS = ["remote", "hybrid", "any"]


@dataclass
class MatchFilter:
    max_age_days: int | None = None        # None = any time
    location: str = "any"                  # remote | hybrid | any
    keywords: list[str] = field(default_factory=list)
    exclude_locations: list[str] = field(default_factory=list)  # drop if location matches
    include_locations: list[str] = field(default_factory=list)  # keep-only if set
    sources: list[str] = field(default_factory=list)            # keep-only if set
    # Defaults to True: this filter shapes shortlists you act on (digest, /jobs,
    # /apply), and re-offering a job you already dismissed is the bug. The dashboard
    # sets it False so it can still render the "Dismissed · Undo" row.
    hide_triaged: bool = True
    min_score: float = 0.0
    # Annualised floor. None = no salary filter; rows with unparseable pay are
    # kept, because most postings state none and dropping them would hide the
    # market behind a filter the operator thinks is about money.
    min_salary: float | None = None

    @classmethod
    def from_profile(cls, profile) -> "MatchFilter":
        """Default filter seeded from preferences: remote-only if it's a must-have,
        plus the profile's preferred/excluded locations. This is the persistent default."""
        remote_pref = "remote" in [m.lower() for m in profile.must_haves]
        return cls(
            location="remote" if remote_pref else "any",
            exclude_locations=list(profile.exclude_locations),
            include_locations=list(profile.preferred_locations),
        )


def apply_menu_action(flt: MatchFilter, action: str, value: str) -> MatchFilter:
    """Update the filter from a menu callback. Pure → unit-testable."""
    if action == "date":
        days = int(value)
        flt.max_age_days = days or None
    elif action == "loc":
        flt.location = value if value in LOCATIONS else "any"
    elif action == "kwclear":
        flt.keywords = []
    return flt


def set_keywords(flt: MatchFilter, text: str) -> MatchFilter:
    flt.keywords = [w.strip() for w in (text or "").replace(",", " ").split() if w.strip()]
    return flt


def filter_summary(flt: MatchFilter) -> str:
    age = "any time" if not flt.max_age_days else (
        "today" if flt.max_age_days == 1 else f"last {flt.max_age_days}d"
    )
    kw = ", ".join(flt.keywords) if flt.keywords else "none"
    return f"📅 {age}  ·  📍 {flt.location}  ·  🔤 {kw}"


def is_owner(chat_id: int | None, owner_id: int | None) -> bool:
    """Lock the bot to one chat. If no owner configured, deny everyone (fail closed)."""
    return owner_id is not None and chat_id == owner_id


def ranked_matches(store: Store, n: int = 10, flt: MatchFilter | None = None, offset: int = 0,
                   max_per_company: int | None = 2) -> list[dict]:
    """The diversified, ranked, FILTERED shortlist. /jobs and /apply <rank> share this
    so the numbering a user sees maps to the right job.

    `max_per_company=None` disables the cap. A *shortlist* wants it — one employer with
    forty openings must not fill a top-10. A *browsable list* does not: the cap silently
    drops jobs you still have to decide on, so the dashboard's count would disagree with
    the queue count in stats(). Measured on a real store: 231 strong unstashed matches
    collapsed to 46 under the cap."""
    flt = flt or MatchFilter()
    pool = store.get_matches(
        limit=max(n * 8, 40), min_score=flt.min_score,
        max_age_days=flt.max_age_days, location=flt.location, keywords=flt.keywords,
        exclude_locations=flt.exclude_locations, include_locations=flt.include_locations,
        sources=flt.sources, hide_triaged=flt.hide_triaged, offset=offset,
        min_salary=flt.min_salary,
    )
    if max_per_company is None:
        return pool[:n]
    return diversify(pool, n, max_per_company=max_per_company)


def jobs_text(store: Store, n: int = 10, flt: MatchFilter | None = None) -> str:
    return format_matches(ranked_matches(store, n, flt))


def resolve_ranked_job(store: Store, rank: int, flt: MatchFilter | None = None, pool: int = 25) -> dict | None:
    """Map a 1-based rank (as shown by /jobs) to its job dict, or None if out of range."""
    ranked = ranked_matches(store, pool, flt)
    if rank < 1 or rank > len(ranked):
        return None
    return ranked[rank - 1]


def apply_callback_data(action: str, app_id: str) -> str:
    return f"{action}:{app_id}"


def parse_callback_data(data: str) -> tuple[str, str]:
    action, _, app_id = (data or "").partition(":")
    return action, app_id


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n].rstrip() + "…"


def apply_preview_text(bundle) -> str:
    """Telegram-friendly review of a drafted application (CV is sent separately as a file)."""
    job = bundle.job
    lines = [
        f"📝 *Draft application* — {job.get('title')} @ {job.get('company')}",
        f"Method: {bundle.apply_method}",
        "",
        f"✉️ *Subject:* {bundle.email_subject}",
        _truncate(bundle.email_body, 800),
        "",
        "📄 *Cover letter:*",
        _truncate(bundle.cover_letter, 900),
        "",
        f"📎 Tailored CV ({len(bundle.cv_markdown)} chars) attached above.",
    ]
    ats = getattr(bundle, "ats", None)
    if ats is not None:
        lines.append(f"🔎 {ats.summary()}")
    if bundle.apply_method != "email":
        lines.append("\n⚠️ Not an email posting — approving hands you the apply link (Tier-2 form-fill is Phase 4).")
    lines.append("\nApprove to send?")
    return "\n".join(lines)


def upskill_report_for(db_path: str, *, min_score: float = 0.5, limit: int = 12) -> dict:
    """Open a Store, build the gap report, close it. One store per call (R15)."""
    store = Store(db_path)
    try:
        return upskill_report(store, min_score=min_score, limit=limit)
    finally:
        store.close()


def upskill_text(report: dict) -> str:
    """Plain-text skill-gap heatmap for Telegram. No Markdown — gap labels come from the
    hunt and may contain characters (underscores, dots) that break legacy Markdown."""
    gaps = report.get("gaps", [])
    n = report.get("n_jobs", 0)
    floor = int(round(float(report.get("min_score", 0.5)) * 100))
    lines = [f"🎯 Skill gaps across {n} match(es) with fit ≥ {floor}%", ""]
    if not gaps:
        lines.append("No recurring skill gaps found. Score jobs with /jobs first, "
                     "or lower the fit floor, e.g. /upskill 0.4")
        return "\n".join(lines)
    peak = gaps[0]["count"] or 1
    for g in gaps:
        bar = "█" * max(1, round(g["count"] / peak * 10))
        lines.append(f"• {g['skill']} — {bar} {g['count']} job(s)")
    structural = report.get("structural") or {}
    if structural:
        lines.append("")
        lines.append("Non-skill filters: "
                     + ", ".join(f"{k} ({v})" for k, v in structural.items()))
    lines.append("\nFull learning plan:  make upskill")
    return "\n".join(lines)


def status_text(stats: dict) -> str:
    by_source = stats.get("by_source", {})
    src_lines = "\n".join(f"   • {s}: {n}" for s, n in by_source.items()) or "   (none)"
    last = stats.get("last_ingest") or "never"
    return (
        "📊 *Pipeline status*\n"
        f"Total jobs: {stats.get('total_jobs', 0)}\n"
        f"Scored matches: {stats.get('matches', 0)} "
        f"(strong ≥70%: {stats.get('strong_matches', 0)})\n"
        f"Last ingest: {last}\n"
        f"By sense:\n{src_lines}"
    )
