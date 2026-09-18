"""Format the top matches into a human-readable shortlist. Used by the match CLI
now and the Telegram bot in the next step."""

from __future__ import annotations

import json


def diversify(matches: list[dict], limit: int, max_per_company: int = 2) -> list[dict]:
    """Cap how many roles from one company appear, so a single employer with many
    near-identical openings doesn't flood the shortlist."""
    seen: dict[str, int] = {}
    out = []
    for m in matches:
        key = (m.get("company") or m.get("source") or "").lower()
        if seen.get(key, 0) >= max_per_company:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(m)
        if len(out) >= limit:
            break
    return out


def format_matches(matches: list[dict]) -> str:
    """Format an already-prepared (diversified, limited) list. Numbering here matches
    /apply <rank>, so callers must pass the same list they show the user."""
    if not matches:
        return "Nothing in the catch yet. Run ingestion + hunting first."
    lines = [f"🎯 Your catch — top {len(matches)} matches\n"]
    for i, m in enumerate(matches, 1):
        pct = int(round(m["score"] * 100))
        company = m.get("company") or m.get("source")
        loc = m.get("location") or ("Remote" if m.get("is_remote") else "—")
        link = m.get("apply_url") or m.get("url") or ""
        try:
            gaps = json.loads(m.get("gaps") or "[]")
        except (json.JSONDecodeError, TypeError):
            gaps = []
        lines.append(f"{i}. [{pct}%] {m['title']} — {company} ({loc})")
        if m.get("rationale"):
            lines.append(f"    ↳ {m['rationale']}")
        if gaps:
            lines.append(f"    ⚠ {'; '.join(gaps)}")
        if link:
            lines.append(f"    {link}")
    return "\n".join(lines)


def format_digest(matches: list[dict], limit: int = 10, max_per_company: int = 2) -> str:
    """Convenience: diversify + limit + format in one call."""
    return format_matches(diversify(matches, limit, max_per_company))


def health_banner(report, health: dict, *, gap_hours: float | None = None,
                  expected_every_hours: float = 24.0) -> str:
    """Prefix for the digest describing anything degraded about this run.

    A digest that never mentions failure is indistinguishable from a healthy one,
    so the daily push doubles as the heartbeat: warnings ride along with it.

    `gap_hours` is the age of the previous successful ingest, measured *before* this
    run started — the caller must capture it first, because by the time this runs the
    store has already been updated and the gap always reads as zero. It is how a
    skipped schedule becomes visible: a daily job whose last run was 90 hours ago
    tells you the scheduler stopped, the first time it comes back.
    """
    lines: list[str] = []
    if gap_hours is not None and gap_hours > expected_every_hours * 1.5:
        days = gap_hours / 24
        lines.append(
            f"⚠️ First successful run in {days:.1f} days — expected every "
            f"{expected_every_hours:.0f}h. The scheduler was not running."
        )
    failed = [r for r in report.results if r.error]
    if failed:
        lines.append(f"⚠️ {len(failed)} sense(s) failed this run:")
        lines += [f"  • {r.source}: {r.error}" for r in failed]
    if report.total_fetched == 0:
        lines.append("⚠️ No postings fetched from any sense — check credentials/network.")
    elif getattr(report, "total_dropped", 0) and report.total_dropped == report.total_fetched:
        # Senses answered but the gate rejected every posting — almost always a
        # mis-set filter, and indistinguishable from a dead pipeline without this.
        lines.append(f"⚠️ The ingest gate filtered out all {report.total_fetched} fetched "
                     f"postings ({report.drops_by_reason}) — check your ingest filters.")
    stale = [s["source"] for s in health.get("sources", []) if (s.get("hours_since") or 0) > 48]
    if stale:
        lines.append(f"⚠️ No new data in 48h+ from: {', '.join(stale)}")
    return "\n".join(lines) + "\n\n" if lines else ""


def format_followups(pending: list[dict]) -> str:
    """A 'these have gone quiet' block for the digest.

    Empty when nothing is waiting, so the digest gains no routine noise. Drafting the
    actual nudge is a separate, explicit action — this only tells you it is time.
    """
    if not pending:
        return ""
    lines = [f"\n\n\u23f0 {len(pending)} application(s) awaiting a reply:"]
    for p in pending:
        company = p.get("company") or "?"
        lines.append(f"  \u2022 {p.get('title')} \u2014 {company} ({p.get('days_waiting')}d)")
    return "\n".join(lines)
