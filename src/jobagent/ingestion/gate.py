"""Ingest gate — decide what is worth *storing*, before it reaches the store.

The boards cannot filter server-side: RemoteOK, Greenhouse, Lever and Ashby hand back
whole feeds, so the download happens regardless. What this saves is store growth and
matching time — every stored job is re-scored on every pass, so a store that only holds
plausible jobs makes the whole pipeline cheaper.

TENSION WITH R4 ("never discard source data"), stated plainly: R4 is about not dropping
*fields* from a job you keep. This drops whole postings, which is a different axis — and
it is irreversible, unlike scoring. So the gate is deliberately limited to cheap, stable
facts the user will not change their mind about (age, location, hard exclusions) and
never to fit judgment (skills, seniority, requirements): the scorer already handles
those, reversibly, for free. Every drop is counted per reason and surfaced in the ingest
event and the run summary, so a mis-set gate shows up as "481 filtered" rather than as a
mysteriously empty queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# The selectable set for the dashboard, in display order. `aggregator` is listed because
# the toggle exists, but it has no sense yet — see docs/ARCHITECTURE.md.
ALL_SOURCES = ["remoteok", "remotive", "himalayas", "greenhouse", "lever", "ashby", "telegram", "aggregator"]

# Terms that mean "remote" as a structured fact rather than a place name.
_REMOTE_WORDS = ("remote", "worldwide", "anywhere", "distributed")


def _split(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _hits(terms: list[str], hay: str) -> bool:
    """Word-boundary match. Substring matching here would silently drop good jobs:
    a location term of "US" matches "Belarus" and "Austria", and this codebase has
    already been bitten twice by exactly that ("Go" in "going", "cto" in "Connectors")."""
    for t in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", hay):
            return True
    return False


@dataclass
class IngestGate:
    """Reject postings before they are stored. All-empty = keep everything."""

    max_age_days: int | None = None
    locations: list[str] = field(default_factory=list)
    drop_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings) -> IngestGate:
        days = getattr(settings, "ingest_max_age_days", 0) or 0
        return cls(
            max_age_days=int(days) if int(days) > 0 else None,
            locations=_split(getattr(settings, "ingest_locations", "")),
            drop_keywords=_split(getattr(settings, "ingest_drop_keywords", "")),
        )

    @property
    def active(self) -> bool:
        return bool(self.max_age_days or self.locations or self.drop_keywords)

    def describe(self) -> str:
        if not self.active:
            return "off (storing everything)"
        parts = []
        if self.max_age_days:
            parts.append(f"≤{self.max_age_days}d old")
        if self.locations:
            parts.append("location in [" + ", ".join(self.locations) + "]")
        if self.drop_keywords:
            parts.append(f"{len(self.drop_keywords)} drop-keyword(s)")
        return "; ".join(parts)

    def reject(self, job) -> str | None:
        """Return a short drop reason, or None to keep the posting.

        The reason is the label counted in the ledger, so keep the set small and stable.
        """
        if self.max_age_days and job.posted_at is not None:
            # No posted_at → keep. Telegram posts and some boards omit it, and dropping
            # unknown-age jobs would quietly delete an entire sense's output.
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
            posted = job.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted < cutoff:
                return "too_old"

        if self.locations:
            location = (job.location or "").lower()
            title = (job.title or "").lower()
            wants_remote = any(w in _REMOTE_WORDS for w in (t.lower() for t in self.locations))
            # Trust the structured flag for "remote" — the word itself is buried in
            # every posting's culture boilerplate, so text matching over-accepts.
            if wants_remote and job.is_remote:
                pass
            elif not _hits(self.locations, f"{location} {title}"):
                return "location"

        if self.drop_keywords:
            # `job` is a JobPosting here (pre-storage), so tags is a real list.
            tags = " ".join(str(t) for t in (job.tags or []))
            haystack = f"{job.title or ''} {job.description or ''} {tags}".lower()
            if _hits(self.drop_keywords, haystack):
                return "keyword"

        return None


def resolve_sources(settings, toml_sources) -> set[str]:
    """Which sense slugs may run this pass.

    `ingest_sources` (Settings/dashboard) wins when set, because it is the surface the
    user just edited; an empty value falls back to `[sources]` in preferences.toml so
    existing installs are unchanged. Returning a set keeps the caller's ordering.
    """
    chosen = _split(getattr(settings, "ingest_sources", ""))
    if chosen:
        return {s.lower() for s in chosen}
    return {s for s in ALL_SOURCES if toml_sources.is_enabled(s)}
